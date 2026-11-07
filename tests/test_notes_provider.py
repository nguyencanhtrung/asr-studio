from __future__ import annotations

import copy
import dataclasses
import json
import re
import threading
import time
from dataclasses import dataclass

import pytest

from asr_studio.chatgpt import (
    ACCESS_REFRESH_MARGIN_SECONDS,
    DIRECT_SCOPE,
    NOTES_HTTP_READ_TIMEOUT_SECONDS,
    NOTES_HTTP_TOTAL_TIMEOUT_SECONDS,
    ChatGPTError,
    ChatGPTProvider,
)
from asr_studio.notes_document import (
    MAX_OUTPUT_BYTES,
    MAX_OUTPUT_RECOVERY_ATTEMPTS,
    MAX_OUTPUT_RECOVERY_CALLS,
    MAX_PROMPT_BYTES,
    MAX_REDUCTION_OUTPUT_BYTES,
    MAX_REDUCTION_TARGET_BYTES,
    notes_schema_json_bytes,
)

# The visible mark of a prompt that asks again for a smaller answer.
RECOVERY_MARK = "previous answer exceeded the output size limit"


@dataclass
class NotesReply:
    prompt: str
    access_token: str
    model: str
    reasoning_effort: str
    allowed_source_ids: tuple[str, ...]
    max_output_bytes: int | None


class NotesOnlyAdapter:
    def __init__(
        self,
        replies: list[str | ChatGPTError] | None = None,
        *,
        responder=None,
    ) -> None:
        self.replies = list(replies or [])
        self.responder = responder
        self.calls: list[NotesReply] = []
        self.refresh_calls: list[dict[str, str]] = []
        self.refresh_results: list[dict | ChatGPTError] = []

    def refresh(self, *, client_id, refresh_token):
        self.refresh_calls.append({"client_id": client_id, "refresh_token": refresh_token})
        if self.refresh_results:
            result = self.refresh_results.pop(0)
            if isinstance(result, ChatGPTError):
                raise result
            return result
        return {
            "access_token": "refreshed-test-access-token",
            "refresh_token": "refreshed-test-refresh-token",
            "token_type": "Bearer",
            "expires_in": 3600,
            "scope": DIRECT_SCOPE,
        }

    def generate_notes(
        self,
        *,
        access_token,
        model,
        reasoning_effort,
        prompt,
        allowed_source_ids,
        max_output_bytes=None,
        budget=None,
    ):
        self.calls.append(
            NotesReply(
                prompt,
                access_token,
                model,
                reasoning_effort,
                tuple(allowed_source_ids),
                max_output_bytes,
            )
        )
        if self.responder is not None:
            return self.responder(prompt, max_output_bytes)
        reply = self.replies.pop(0)
        if isinstance(reply, ChatGPTError):
            raise reply
        return reply


def _provider(tmp_path, adapter, *, clock=time.time):
    provider = ChatGPTProvider(tmp_path / "provider", callback_url="", adapter=adapter, clock=clock)
    provider.ensure()
    client_id = "oaiapp_test-123456"
    with provider.store.locked(write=True) as state:
        state["connection_status"] = "connected"
        state["connection_version"] = 7
        state["active_client_id"] = client_id
        state["registrations"][client_id] = {
            "access_token": "local-test-access-token",
            "refresh_token": "local-test-refresh-token",
            "expires_at": provider._now() + 3600,
            "scopes": [DIRECT_SCOPE],
            "subject": "test-subject",
            "label": "Test account",
        }
        state["notes_provider"] = "chatgpt"
        state["selected_model"] = "gpt-6.1-sol"
        state["selected_reasoning_effort"] = "high"
    return provider


def _snapshot():
    return {
        "id": "a" * 32,
        "meeting_id": "b" * 32,
        "meeting_date": "2026-10-19",
        "segments": [
            {
                "id": "c" * 32,
                "start_seconds": 1.0,
                "end_seconds": 2.0,
                "text": "Morgan will prepare the draft.",
                "speaker_name": "Morgan",
            }
        ],
    }


def _valid_document():
    return {
        "schema_version": 1,
        "overview": [{"text": "Morgan will prepare a draft.", "source_ids": ["00000"]}],
        "key_topics": [],
        "decisions": [],
        "open_questions": [],
        "action_items": [],
        "confirmation_items": [],
    }


def _chunked_snapshot():
    snapshot = _snapshot()
    snapshot["segments"] = [
        {
            "id": f"{index:032x}",
            "start_seconds": float(index),
            "end_seconds": float(index + 1),
            "text": "x" * 16_000,
            "speaker_name": "Morgan",
        }
        for index in (1, 2)
    ]
    return snapshot


def _payload(prompt, marker):
    """The JSON after the marker; a recovery or repair suffix may follow it."""
    return json.JSONDecoder().raw_decode(prompt.split(marker, 1)[1])[0]


def _document_for_prompt(prompt):
    if "SOURCE_JSON:\n" in prompt:
        payload = _payload(prompt, "SOURCE_JSON:\n")
        source_ids = [payload["segments"][0]["id"]]
    else:
        payload = _payload(prompt, "SYNTHESIS_INPUT_JSON:\n")
        source_ids = list(
            dict.fromkeys(
                source_id
                for summary in payload["summaries"]
                for source_id in summary["source_order"]
            )
        )
    document = _valid_document()
    document["overview"] = [{"text": "A concise summary.", "source_ids": source_ids[:1]}]
    return json.dumps(document)


class MutableClock:
    def __init__(self, value=1_800_000_000):
        self.value = float(value)

    def __call__(self):
        return self.value


def test_invalid_first_result_gets_one_bounded_repair_on_the_pinned_account(tmp_path):
    from asr_studio.notes_document import build_notes_schema

    adapter = NotesOnlyAdapter(["not-json", json.dumps(_valid_document())])
    provider = _provider(tmp_path, adapter)

    result = provider.generate_notes_document(
        snapshot=_snapshot(),
        connection_version=7,
        model="gpt-6.1-sol",
        reasoning_effort="high",
    )

    assert result["overview"][0]["text"] == "Morgan will prepare a draft."
    assert len(adapter.calls) == 2
    assert adapter.calls[0].prompt != adapter.calls[1].prompt
    assert all(call.allowed_source_ids == ("00000",) for call in adapter.calls)
    assert "c" * 32 not in adapter.calls[0].prompt
    assert all(len(call.prompt.encode("utf-8")) <= MAX_PROMPT_BYTES for call in adapter.calls)
    assert all(call.access_token == "local-test-access-token" for call in adapter.calls)
    assert all(call.model == "gpt-6.1-sol" for call in adapter.calls)
    assert all(call.reasoning_effort == "high" for call in adapter.calls)
    assert all(call.max_output_bytes == 128_000 for call in adapter.calls)
    for call in adapter.calls:
        schema = build_notes_schema(call.allowed_source_ids)
        schema_bytes = len(
            json.dumps(schema, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
        assert len(call.prompt.encode("utf-8")) + schema_bytes + 1_024 <= 35_000
    assert provider.store._read().get("last_diagnostic") is None


def test_notes_progress_reports_pipeline_stages_without_replacing_coverage(tmp_path):
    adapter = NotesOnlyAdapter([json.dumps(_valid_document())])
    provider = _provider(tmp_path, adapter)
    progress = []
    coverage = []

    result = provider.generate_notes_document(
        snapshot=_snapshot(),
        connection_version=7,
        model="gpt-6.1-sol",
        reasoning_effort="high",
        on_progress=progress.append,
        on_coverage=coverage.append,
    )

    assert result["overview"]
    assert [event["stage"] for event in progress] == [
        "preparing_source",
        "generating",
        "generating",
    ]
    assert progress[1]["current"] == 0 and progress[1]["total"] == 1
    assert progress[-1]["current"] == 1 and progress[-1]["total"] == 1
    assert all(set(event) <= {"stage", "current", "total", "unit"} for event in progress)
    assert coverage and coverage[0]["provider_calls"] == 1
    assert NOTES_HTTP_READ_TIMEOUT_SECONDS < 300


def test_chunk_progress_is_before_each_call_and_after_each_validated_chunk(tmp_path, monkeypatch):
    from asr_studio import notes_document

    monkeypatch.setattr(notes_document, "MAX_PRIMARY_PROMPT_BYTES", 20_000)
    progress = []
    observations = []
    adapter = NotesOnlyAdapter()

    def respond(prompt, _max_output_bytes):
        # The adapter is entered immediately before provider inference. A chunk must still be
        # pending at that point; a total-sized event here would falsely announce completion.
        observations.append(("SOURCE_JSON:\n" in prompt, [dict(event) for event in progress]))
        return _document_for_prompt(prompt)

    adapter.responder = respond
    provider = _provider(tmp_path, adapter)
    provider.generate_notes_document(
        snapshot=_chunked_snapshot(),
        connection_version=7,
        model="gpt-6.1-sol",
        reasoning_effort="high",
        on_progress=progress.append,
    )

    chunk_calls = [events for is_chunk, events in observations if is_chunk]
    assert len(chunk_calls) >= 2
    assert [events[-1]["current"] for events in chunk_calls[:2]] == [0, 1]
    chunk_events = [event for event in progress if event["stage"] == "chunking"]
    assert chunk_events[-1] == {"stage": "chunking", "current": 2, "total": 2, "unit": "chunks"}


def test_vietnamese_generation_localizes_chunk_repair_reduction_and_final_prompts(tmp_path):
    from asr_studio.notes_document import NotesDocumentError

    snapshot = _snapshot()
    snapshot["segments"] = [
        {
            "id": f"{index:032x}",
            "start_seconds": float(index),
            "end_seconds": float(index) + 1,
            "text": "x" * 16_000,
            "speaker_name": "Morgan",
        }
        for index in range(4)
    ]
    calls = 0

    def respond(prompt, _max_output_bytes):
        nonlocal calls
        calls += 1
        if calls == 1:
            return "not-json"
        if "SOURCE_JSON:\n" in prompt:
            payload = json.loads(prompt.split("SOURCE_JSON:\n", 1)[1].splitlines()[0])
            source_ids = [payload["segments"][0]["id"]]
            document = _valid_document()
            document["overview"] = [
                {"text": "Bản tóm tắt " + "x" * 6_800, "source_ids": source_ids}
            ]
        else:
            payload = json.loads(prompt.split("SYNTHESIS_INPUT_JSON:\n", 1)[1].splitlines()[0])
            source_ids = list(
                dict.fromkeys(
                    source_id
                    for summary in payload["summaries"]
                    for source_id in summary["source_order"]
                )
            )
            document = _valid_document()
            document["overview"] = [{"text": "Tổng hợp ngắn gọn.", "source_ids": source_ids[:1]}]
        return json.dumps(document, ensure_ascii=False, separators=(",", ":"))

    adapter = NotesOnlyAdapter(responder=respond)
    provider = _provider(tmp_path, adapter)
    result = provider.generate_notes_document(
        snapshot=snapshot,
        connection_version=7,
        model="gpt-6.1-sol",
        reasoning_effort="high",
        output_language="vi",
    )

    prompts = [call.prompt for call in adapter.calls]
    assert len(adapter.calls) == 6
    assert result["overview"][0]["text"] == "Tổng hợp ngắn gọn."
    assert all("tiếng Việt" in prompt for prompt in prompts)
    assert any("Hãy sửa đầu ra không hợp lệ" in prompt for prompt in prompts)
    assert any("Tạo ghi chú cuối cùng" in prompt for prompt in prompts)
    assert all(len(prompt.encode("utf-8")) <= MAX_PROMPT_BYTES for prompt in prompts)

    no_calls = NotesOnlyAdapter([json.dumps(_valid_document())])
    invalid_provider = _provider(tmp_path / "invalid", no_calls)
    with pytest.raises(NotesDocumentError):
        invalid_provider.generate_notes_document(
            snapshot=_snapshot(),
            connection_version=7,
            model="gpt-6.1-sol",
            reasoning_effort="high",
            output_language=None,
        )
    assert no_calls.calls == []


def test_invalid_repair_fails_without_a_third_provider_call(tmp_path):
    adapter = NotesOnlyAdapter(["not-json", "still-not-json"])
    provider = _provider(tmp_path, adapter)

    with pytest.raises(ChatGPTError) as invalid:
        provider.generate_notes_document(
            snapshot=_snapshot(),
            connection_version=7,
            model="gpt-6.1-sol",
            reasoning_effort="high",
        )

    assert invalid.value.code == "notes_invalid_output"
    assert len(adapter.calls) == 2


def test_provider_failure_is_not_retried_as_a_repair(tmp_path):
    adapter = NotesOnlyAdapter(
        [ChatGPTError(503, "provider_timeout", "ChatGPT did not complete the notes request.")]
    )
    provider = _provider(tmp_path, adapter)

    with pytest.raises(ChatGPTError) as failed:
        provider.generate_notes_document(
            snapshot=_snapshot(),
            connection_version=7,
            model="gpt-6.1-sol",
            reasoning_effort="high",
        )

    assert failed.value.code == "provider_timeout"
    assert len(adapter.calls) == 1


def test_compact_near_limit_document_is_accepted_without_schema_repair(tmp_path, monkeypatch):
    from asr_studio import notes_document

    output_budget = 8_000
    compact_budget = 7_985
    document = _valid_document()
    snapshot = _snapshot()
    snapshot["segments"][0]["id"] = "c0000"
    document["overview"][0]["source_ids"] = ["c0000"]
    document["overview"][0]["text"] = ""
    template = json.dumps(document, ensure_ascii=False, separators=(",", ":"))
    document["overview"][0]["text"] = "x" * (compact_budget - len(template.encode("utf-8")))
    compact_output = json.dumps(document, ensure_ascii=False, separators=(",", ":"))

    assert len(compact_output.encode("utf-8")) == compact_budget
    assert len(json.dumps(json.loads(compact_output), ensure_ascii=False).encode("utf-8")) == (
        output_budget + 1
    )
    monkeypatch.setattr(notes_document, "MAX_OUTPUT_BYTES", output_budget)
    adapter = NotesOnlyAdapter([compact_output, compact_output])
    provider = _provider(tmp_path, adapter)

    result = provider.generate_notes_document(
        snapshot=snapshot,
        connection_version=7,
        model="gpt-6.1-sol",
        reasoning_effort="high",
    )

    assert len(result["overview"][0]["text"]) > 7_000
    assert len(adapter.calls) == 1
    assert adapter.calls[0].max_output_bytes == output_budget
    assert "INVALID_OUTPUT:" not in adapter.calls[0].prompt


def test_invalid_citation_terminal_failure_is_safe_and_uses_only_one_repair(tmp_path):
    invalid_document = _valid_document()
    invalid_document["overview"][0]["source_ids"] = ["private-unknown-source-id"]
    invalid_output = json.dumps(invalid_document)
    adapter = NotesOnlyAdapter([invalid_output, invalid_output])
    provider = _provider(tmp_path, adapter)

    with pytest.raises(ChatGPTError) as invalid:
        provider.generate_notes_document(
            snapshot=_snapshot(),
            connection_version=7,
            model="gpt-6.1-sol",
            reasoning_effort="high",
        )

    assert invalid.value.code == "notes_invalid_output"
    assert "source references" in invalid.value.public_message
    assert "private-unknown-source-id" not in invalid.value.public_message
    assert len(adapter.calls) == 2
    diagnostic = provider.store._read()["last_diagnostic"]
    assert diagnostic["validation_reason"] == "source_ids"
    assert diagnostic["stage"] == "single"
    assert diagnostic["provider_call_index"] == 2
    assert diagnostic["attempt"] == "repair"
    assert diagnostic["output_bytes"] == len(invalid_output.encode("utf-8"))
    assert diagnostic["repair_state"] == "already_consumed"
    assert "private-unknown-source-id" not in json.dumps(diagnostic)


def test_provider_normalizes_only_valid_duplicate_citations_without_repair(tmp_path):
    document = _valid_document()
    document["overview"][0]["source_ids"] = ["00000", "00000"]
    adapter = NotesOnlyAdapter([json.dumps(document), json.dumps(_valid_document())])
    provider = _provider(tmp_path, adapter)

    result = provider.generate_notes_document(
        snapshot=_snapshot(),
        connection_version=7,
        model="gpt-6.1-sol",
        reasoning_effort="high",
    )

    assert len(adapter.calls) == 1
    assert result["overview"][0]["source_ids"] == ["c" * 32]
    assert adapter.calls[0].allowed_source_ids == ("00000",)


def test_final_output_expansion_fails_safely_after_restoring_original_ids(tmp_path):
    from asr_studio.notes_document import MAX_OUTPUT_BYTES, project_source_snapshot

    snapshot = _snapshot()
    snapshot["segments"] = [
        {
            "id": f"segment-{index:04d}-" + "x" * 124,
            "start_seconds": float(index),
            "end_seconds": float(index + 1),
            "text": "source",
        }
        for index in range(200)
    ]
    projection = project_source_snapshot(snapshot)
    alias_ids = list(projection.alias_to_original)
    document = {
        "schema_version": 1,
        **{
            key: []
            for key in (
                "overview",
                "key_topics",
                "decisions",
                "open_questions",
                "action_items",
                "confirmation_items",
            )
        },
        "overview": [
            {
                "text": "x" * 11_500,
                "source_ids": alias_ids[index * 20 : (index + 1) * 20],
            }
            for index in range(10)
        ],
    }
    output = json.dumps(document, ensure_ascii=False, separators=(",", ":"))
    assert len(output.encode("utf-8")) <= MAX_OUTPUT_BYTES
    adapter = NotesOnlyAdapter([output])
    provider = _provider(tmp_path, adapter)

    result = provider.generate_notes_document(
        snapshot=snapshot,
        connection_version=7,
        model="gpt-6.1-sol",
        reasoning_effort="high",
    )

    assert len(adapter.calls) == 1
    assert len(result["overview"]) == 1
    assert len(result["overview"][0]["source_ids"]) == len(snapshot["segments"])
    assert set(result["overview"][0]["source_ids"]) == {
        segment["id"] for segment in snapshot["segments"]
    }


@pytest.mark.parametrize(
    ("failure_kind", "expected_detail"),
    [
        ("unknown", "unknown_source_id"),
        ("missing", "required_source_missing"),
    ],
)
def test_terminal_source_diagnostics_distinguish_failures_without_content(
    tmp_path, failure_kind, expected_detail
):
    document = _valid_document()
    document["overview"][0]["text"] = "private summary content"
    if failure_kind == "unknown":
        document["overview"][0]["source_ids"] = ["private-unknown-source-id"]
    else:
        document["decisions"] = [{"text": "private decision content", "source_ids": []}]
    invalid_output = json.dumps(document)
    adapter = NotesOnlyAdapter([invalid_output, invalid_output])
    provider = _provider(tmp_path, adapter)

    with pytest.raises(ChatGPTError) as invalid:
        provider.generate_notes_document(
            snapshot=_snapshot(),
            connection_version=7,
            model="gpt-6.1-sol",
            reasoning_effort="high",
        )

    assert invalid.value.code == "notes_invalid_output"
    assert invalid.value.public_message == (
        "ChatGPT returned notes with invalid source references. No new notes were saved."
    )
    assert len(adapter.calls) == 2
    diagnostic = provider.store._read()["last_diagnostic"]
    assert diagnostic["validation_reason"] == "source_ids"
    assert diagnostic["validation_detail"] == expected_detail
    serialized_diagnostic = json.dumps(diagnostic)
    assert "private" not in serialized_diagnostic
    assert "c" * 32 not in serialized_diagnostic


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("owner", {"secret": "private owner value"}),
        ("deadline_text", "private deadline phrase\x01"),
        ("deadline_date", {"secret": "private date value"}),
    ],
)
def test_action_field_diagnostics_keep_only_allowlisted_field_name(
    tmp_path, field_name, invalid_value
):
    document = _valid_document()
    action = {
        "text": "private action content",
        "source_ids": ["00000"],
        "owner": None,
        "deadline_text": None,
        "deadline_date": None,
    }
    action[field_name] = invalid_value
    document["action_items"] = [action]
    invalid_output = json.dumps(document)
    adapter = NotesOnlyAdapter([invalid_output, invalid_output])
    provider = _provider(tmp_path, adapter)

    with pytest.raises(ChatGPTError) as invalid:
        provider.generate_notes_document(
            snapshot=_snapshot(),
            connection_version=7,
            model="gpt-6.1-sol",
            reasoning_effort="high",
        )

    assert invalid.value.code == "notes_invalid_output"
    assert len(adapter.calls) == 2
    diagnostic = provider.store._read()["last_diagnostic"]
    assert diagnostic["validation_reason"] == "action_fields"
    assert diagnostic["validation_field"] == field_name
    serialized_diagnostic = json.dumps(diagnostic)
    assert "private" not in serialized_diagnostic
    assert "c" * 32 not in serialized_diagnostic


def test_invalid_output_keeps_safe_diagnostic_when_repair_cannot_fit(tmp_path):
    invalid_output = "not-json" + "x" * 8_001
    adapter = NotesOnlyAdapter([invalid_output])
    provider = _provider(tmp_path, adapter)

    with pytest.raises(ChatGPTError) as invalid:
        provider.generate_notes_document(
            snapshot=_snapshot(),
            connection_version=7,
            model="gpt-6.1-sol",
            reasoning_effort="high",
        )

    assert invalid.value.code == "notes_invalid_output"
    assert len(adapter.calls) == 1
    diagnostic = provider.store._read()["last_diagnostic"]
    assert diagnostic["validation_reason"] == "json"
    assert diagnostic["stage"] == "single"
    assert diagnostic["provider_call_index"] == 1
    assert diagnostic["attempt"] == "primary"
    assert diagnostic["output_bytes"] == len(invalid_output.encode("utf-8"))
    assert diagnostic["repair_state"] == "unavailable"
    assert diagnostic["prompt_bytes"] == len(adapter.calls[0].prompt.encode("utf-8"))
    assert diagnostic["response_limit"] == 128_000
    assert "not-json" not in json.dumps(diagnostic)


def test_deeply_nested_provider_json_is_normalized_as_invalid_output(tmp_path):
    valid = json.dumps(_valid_document(), separators=(",", ":"))
    start = valid.index('"overview":') + len('"overview":')
    end = valid.index(',"key_topics"', start)
    nested_array = "[" * 1_200 + "0" + "]" * 1_200
    invalid_output = valid[:start] + "[" + nested_array + "]" + valid[end:]
    adapter = NotesOnlyAdapter([invalid_output, invalid_output])
    provider = _provider(tmp_path, adapter)

    with pytest.raises(ChatGPTError) as invalid:
        provider.generate_notes_document(
            snapshot=_snapshot(),
            connection_version=7,
            model="gpt-6.1-sol",
            reasoning_effort="high",
        )

    assert invalid.value.code == "notes_invalid_output"
    assert len(adapter.calls) == 2
    diagnostic = provider.store._read()["last_diagnostic"]
    assert diagnostic["validation_reason"] == "json"
    assert diagnostic["stage"] == "single"
    assert diagnostic["provider_call_index"] == 2
    assert diagnostic["attempt"] == "repair"
    assert diagnostic["output_bytes"] == len(invalid_output.encode("utf-8"))
    assert diagnostic["repair_state"] == "already_consumed"


def test_notes_refreshes_access_token_between_stages_and_keeps_the_task_pin(tmp_path, monkeypatch):
    from asr_studio import notes_document

    monkeypatch.setattr(notes_document, "MAX_PRIMARY_PROMPT_BYTES", 20_000)
    clock = MutableClock()
    adapter = NotesOnlyAdapter(responder=lambda prompt, _limit: _document_for_prompt(prompt))
    provider = _provider(tmp_path, adapter, clock=clock)
    with provider.store.locked(write=True) as state:
        registration = state["registrations"]["oaiapp_test-123456"]
        registration["expires_at"] = clock.value + 1_800

    def advance_each_stage(prompt, _limit):
        clock.value += 700
        return _document_for_prompt(prompt)

    adapter.responder = advance_each_stage
    result = provider.generate_notes_document(
        snapshot=_chunked_snapshot(),
        connection_version=7,
        model="gpt-6.1-sol",
        reasoning_effort="high",
    )

    assert result["overview"]
    assert len(adapter.calls) == 3
    assert [call.access_token for call in adapter.calls] == [
        "local-test-access-token",
        "local-test-access-token",
        "refreshed-test-access-token",
    ]
    assert clock.value - 1_800_000_000 == 2_100
    assert adapter.refresh_calls == [
        {"client_id": "oaiapp_test-123456", "refresh_token": "local-test-refresh-token"}
    ]
    assert all(call.model == "gpt-6.1-sol" for call in adapter.calls)
    assert all(call.reasoning_effort == "high" for call in adapter.calls)
    state = provider.store._read()
    assert state["connection_status"] == "connected"
    assert state["connection_version"] == 7
    assert state["active_client_id"] == "oaiapp_test-123456"
    assert state["selected_model"] == "gpt-6.1-sol"
    assert state["selected_reasoning_effort"] == "high"
    registration = state["registrations"]["oaiapp_test-123456"]
    assert registration["subject"] == "test-subject"
    assert registration["label"] == "Test account"
    assert registration["access_token"] == "refreshed-test-access-token"


def test_notes_proactively_refreshes_when_token_cannot_cover_stage_deadline(tmp_path):
    clock = MutableClock()
    adapter = NotesOnlyAdapter([json.dumps(_valid_document())])
    provider = _provider(tmp_path, adapter, clock=clock)
    with provider.store.locked(write=True) as state:
        state["registrations"]["oaiapp_test-123456"]["expires_at"] = (
            clock.value + NOTES_HTTP_TOTAL_TIMEOUT_SECONDS + ACCESS_REFRESH_MARGIN_SECONDS - 1
        )

    provider.generate_notes_document(
        snapshot=_snapshot(),
        connection_version=7,
        model="gpt-6.1-sol",
        reasoning_effort="high",
    )

    assert len(adapter.refresh_calls) == 1
    assert adapter.calls[0].access_token == "refreshed-test-access-token"


def test_terminal_refresh_failure_stops_notes_without_replaying_inference(tmp_path, monkeypatch):
    from asr_studio import notes_document

    monkeypatch.setattr(notes_document, "MAX_PRIMARY_PROMPT_BYTES", 20_000)
    clock = MutableClock()
    adapter = NotesOnlyAdapter(responder=lambda prompt, _limit: _document_for_prompt(prompt))
    adapter.refresh_results.append(ChatGPTError(400, "invalid_grant", "private refresh detail"))
    provider = _provider(tmp_path, adapter, clock=clock)
    with provider.store.locked(write=True) as state:
        state["registrations"]["oaiapp_test-123456"]["expires_at"] = clock.value + 1_800

    def advance_each_stage(prompt, _limit):
        clock.value += 700
        return _document_for_prompt(prompt)

    adapter.responder = advance_each_stage
    with pytest.raises(ChatGPTError) as failed:
        provider.generate_notes_document(
            snapshot=_chunked_snapshot(),
            connection_version=7,
            model="gpt-6.1-sol",
            reasoning_effort="high",
        )

    assert failed.value.code == "reconnect_required"
    assert len(adapter.calls) == 2
    assert len(adapter.refresh_calls) == 1
    assert clock.value - 1_800_000_000 == 1_400
    state = provider.store._read()
    assert state["connection_status"] == "reconnect_required"
    assert "access_token" not in state["registrations"]["oaiapp_test-123456"]
    assert "refresh_token" not in state["registrations"]["oaiapp_test-123456"]


def test_refresh_auth_failure_marks_reconnect_once_before_any_inference(tmp_path):
    clock = MutableClock()
    adapter = NotesOnlyAdapter([json.dumps(_valid_document())])
    adapter.refresh_results.append(
        ChatGPTError(401, "provider_auth_error", "private refresh authorization detail")
    )
    provider = _provider(tmp_path, adapter, clock=clock)
    with provider.store.locked(write=True) as state:
        state["registrations"]["oaiapp_test-123456"]["expires_at"] = (
            clock.value + NOTES_HTTP_TOTAL_TIMEOUT_SECONDS + ACCESS_REFRESH_MARGIN_SECONDS - 1
        )

    with pytest.raises(ChatGPTError) as failed:
        provider.generate_notes_document(
            snapshot=_snapshot(),
            connection_version=7,
            model="gpt-6.1-sol",
            reasoning_effort="high",
        )

    assert failed.value.code == "provider_auth_error"
    assert adapter.calls == []
    assert len(adapter.refresh_calls) == 1
    state = provider.store._read()
    assert state["connection_status"] == "reconnect_required"
    assert state["connection_version"] == 8


def test_only_one_schema_repair_is_available_across_all_chunks(tmp_path):
    snapshot = _snapshot()
    snapshot["segments"] = [
        {"id": "c" * 32, "start_seconds": 1, "end_seconds": 2, "text": "x" * 20_000},
        {"id": "d" * 32, "start_seconds": 3, "end_seconds": 4, "text": "y" * 20_000},
    ]
    first_chunk_result = _valid_document()
    first_chunk_result["overview"][0]["source_ids"] = ["00000"]
    adapter = NotesOnlyAdapter(["not-json", json.dumps(first_chunk_result), "still-not-json"])
    provider = _provider(tmp_path, adapter)

    with pytest.raises(ChatGPTError) as invalid:
        provider.generate_notes_document(
            snapshot=snapshot,
            connection_version=7,
            model="gpt-6.1-sol",
            reasoning_effort="high",
        )

    assert invalid.value.code == "notes_invalid_output"
    assert len(adapter.calls) == 3
    assert "INVALID_OUTPUT:" in adapter.calls[1].prompt
    assert "INVALID_OUTPUT:" not in adapter.calls[2].prompt
    assert all(call.max_output_bytes == 8_000 for call in adapter.calls)
    diagnostic = provider.store._read()["last_diagnostic"]
    assert diagnostic["validation_reason"] == "json"
    assert diagnostic["stage"] == "chunk"
    assert diagnostic["provider_call_index"] == 3
    assert diagnostic["attempt"] == "primary"
    assert diagnostic["output_bytes"] == len(b"still-not-json")
    assert diagnostic["repair_state"] == "already_consumed"


def test_reducer_scope_contains_only_aliases_cited_by_its_inputs(tmp_path):
    snapshot = _snapshot()
    snapshot["segments"] = [
        {"id": "c" * 32, "start_seconds": 1, "end_seconds": 2, "text": "x" * 20_000},
        {"id": "d" * 32, "start_seconds": 3, "end_seconds": 4, "text": "y" * 20_000},
    ]
    first_chunk_result = _valid_document()
    first_chunk_result["overview"][0]["source_ids"] = ["00000"]
    empty_chunk_result = {
        "schema_version": 1,
        **{
            key: []
            for key in (
                "overview",
                "key_topics",
                "decisions",
                "open_questions",
                "action_items",
                "confirmation_items",
            )
        },
    }
    invalid_reducer_result = {
        **empty_chunk_result,
        "overview": [{"text": "Unsupported citation", "source_ids": ["00001"]}],
    }
    adapter = NotesOnlyAdapter(
        [
            json.dumps(first_chunk_result),
            json.dumps(empty_chunk_result),
            json.dumps(invalid_reducer_result),
            json.dumps(invalid_reducer_result),
        ]
    )
    provider = _provider(tmp_path, adapter)

    with pytest.raises(ChatGPTError) as invalid:
        provider.generate_notes_document(
            snapshot=snapshot,
            connection_version=7,
            model="gpt-6.1-sol",
            reasoning_effort="high",
        )

    assert invalid.value.code == "notes_invalid_output"
    assert len(adapter.calls) == 4
    assert adapter.calls[0].allowed_source_ids == ("00000",)
    assert adapter.calls[1].allowed_source_ids == ("00001",)
    assert adapter.calls[2].allowed_source_ids == ("00000",)
    assert adapter.calls[3].allowed_source_ids == ("00000",)
    assert "SYNTHESIS_INPUT_JSON:" in adapter.calls[2].prompt
    assert "INVALID_OUTPUT:" in adapter.calls[3].prompt


def test_empty_reducer_scope_is_sent_as_empty_after_uncited_chunk_summaries(tmp_path):
    snapshot = _snapshot()
    snapshot["segments"] = [
        {"id": "c" * 32, "start_seconds": 1, "end_seconds": 2, "text": "x" * 20_000},
        {"id": "d" * 32, "start_seconds": 3, "end_seconds": 4, "text": "y" * 20_000},
    ]
    empty_document = {
        "schema_version": 1,
        **{
            key: []
            for key in (
                "overview",
                "key_topics",
                "decisions",
                "open_questions",
                "action_items",
                "confirmation_items",
            )
        },
    }
    adapter = NotesOnlyAdapter([json.dumps(empty_document)] * 3)
    provider = _provider(tmp_path, adapter)

    result = provider.generate_notes_document(
        snapshot=snapshot,
        connection_version=7,
        model="gpt-6.1-sol",
        reasoning_effort="high",
    )

    assert len(adapter.calls) == 3
    assert [call.allowed_source_ids for call in adapter.calls] == [
        ("00000",),
        ("00001",),
        (),
    ]
    assert result["overview"] == []


def test_changed_saved_model_does_not_change_the_pinned_run_but_a_new_connection_does(tmp_path):
    adapter = NotesOnlyAdapter([json.dumps(_valid_document())])
    provider = _provider(tmp_path, adapter)
    with provider.store.locked(write=True) as state:
        state["selected_model"] = "gpt-6-luna"
        state["selected_reasoning_effort"] = "none"

    provider.generate_notes_document(
        snapshot=_snapshot(),
        connection_version=7,
        model="gpt-6.1-sol",
        reasoning_effort="high",
    )
    assert [(call.model, call.reasoning_effort) for call in adapter.calls] == [
        ("gpt-6.1-sol", "high")
    ]

    with provider.store.locked(write=True) as state:
        state["connection_version"] = 8
    with pytest.raises(ChatGPTError) as changed:
        provider.generate_notes_document(
            snapshot=_snapshot(),
            connection_version=7,
            model="gpt-6.1-sol",
            reasoning_effort="high",
        )
    assert changed.value.code == "connection_changed"
    assert len(adapter.calls) == 1


def test_long_source_uses_bounded_multi_level_reduction_and_later_decision_wins(
    tmp_path, monkeypatch
):
    from asr_studio import notes_document
    from asr_studio.notes_document import (
        MAX_CHUNK_OUTPUT_BYTES,
        MAX_NOTES_CHUNKS,
        project_source_snapshot,
    )

    monkeypatch.setattr(notes_document, "MAX_PRIMARY_PROMPT_BYTES", 20_000)
    snapshot = _snapshot()
    snapshot["segments"] = [
        {
            "id": f"{index + 1:032x}",
            "start_seconds": float(index * 60),
            "end_seconds": float(index * 60 + 30),
            "text": (
                "proposal: postpone launch " + "x" * 16_000
                if index == 0
                else "decision: launch Tuesday " + "x" * 16_000
                if index == 15
                else f"discussion {index} " + "x" * 16_000
            ),
            "speaker_name": "Morgan",
        }
        for index in range(16)
    ]
    reduction_levels: list[int] = []

    def respond(prompt, max_output_bytes):
        if "SOURCE_JSON:\n" in prompt:
            source = json.loads(prompt.split("SOURCE_JSON:\n", 1)[1])
            segment = source["segments"][0]
            index = (
                int(segment["text"].split(maxsplit=2)[1])
                if segment["text"].startswith("discussion")
                else (0 if segment["text"].startswith("proposal") else 15)
            )
            summary_text = f"map-{index:02d}|" + "x" * 6_800
            result = _valid_document()
            result["overview"] = [{"text": summary_text, "source_ids": [segment["id"]]}]
            if index == 0:
                result["confirmation_items"] = [
                    {
                        "text": "Postponing launch was proposed, not decided.",
                        "source_ids": [segment["id"]],
                    }
                ]
            if index == 15:
                result["decisions"] = [
                    {
                        "text": "The team decided to launch Tuesday.",
                        "source_ids": [segment["id"]],
                    }
                ]
            return json.dumps(result, separators=(",", ":"))

        payload = json.loads(prompt.split("SYNTHESIS_INPUT_JSON:\n", 1)[1])
        records = payload["summaries"]
        source_ids = list(
            dict.fromkeys(source_id for record in records for source_id in record["source_order"])
        )
        if "final meeting-level notes" in prompt:
            result = _valid_document()
            result["overview"] = [
                {"text": "Launch timing was discussed.", "source_ids": [source_ids[0]]}
            ]
            result["decisions"] = [
                {
                    "text": (
                        "The later Tuesday decision supersedes the earlier postponement proposal."
                    ),
                    "source_ids": [source_ids[0], source_ids[-1]],
                }
            ]
            return json.dumps(result, separators=(",", ":"))

        input_levels = [
            int(item["text"].split("|", 1)[0].removeprefix("level-"))
            for record in records
            for item in record["notes"]["overview"]
            if item["text"].startswith("level-")
        ]
        level = max(input_levels, default=0) + 1
        reduction_levels.append(level)
        result = _valid_document()
        result["overview"] = [{"text": f"level-{level}|" + "y" * 6_800, "source_ids": source_ids}]
        result["decisions"] = [item for record in records for item in record["notes"]["decisions"]]
        result["confirmation_items"] = [
            item for record in records for item in record["notes"]["confirmation_items"]
        ]
        return json.dumps(result, separators=(",", ":"))

    adapter = NotesOnlyAdapter(responder=respond)
    provider = _provider(tmp_path, adapter)

    result = provider.generate_notes_document(
        snapshot=snapshot,
        connection_version=7,
        model="gpt-6.1-sol",
        reasoning_effort="high",
    )

    map_calls = [call for call in adapter.calls if "SOURCE_JSON:\n" in call.prompt]
    reduction_calls = [call for call in adapter.calls if "SYNTHESIS_INPUT_JSON:\n" in call.prompt]
    assert len(map_calls) == 16
    assert len(reduction_calls) > 2
    assert max(reduction_levels) >= 2
    assert all(call.max_output_bytes == MAX_CHUNK_OUTPUT_BYTES for call in map_calls)
    assert all(call.max_output_bytes == MAX_REDUCTION_OUTPUT_BYTES for call in reduction_calls[:-1])
    assert reduction_calls[-1].max_output_bytes == 128_000
    assert len(adapter.calls) <= 2 * MAX_NOTES_CHUNKS + 1
    for call in reduction_calls:
        records = json.loads(call.prompt.split("SYNTHESIS_INPUT_JSON:\n", 1)[1])["summaries"]
        if "final meeting-level notes" not in call.prompt:
            assert len(records) <= 2
        previous_source_end = -1
        for record in records:
            source_order = list(record["source_order"].values())
            assert source_order == sorted(source_order)
            if source_order:
                assert source_order[0] > previous_source_end
                previous_source_end = source_order[-1]
    first_source_id = snapshot["segments"][0]["id"]
    last_source_id = snapshot["segments"][-1]["id"]
    projection = project_source_snapshot(snapshot)
    original_to_alias = {
        original_id: alias for alias, original_id in projection.alias_to_original.items()
    }
    assert original_to_alias[first_source_id] in map_calls[0].prompt
    assert original_to_alias[last_source_id] not in map_calls[0].prompt
    assert original_to_alias[last_source_id] in map_calls[-1].prompt
    assert original_to_alias[first_source_id] not in map_calls[-1].prompt
    assert first_source_id not in map_calls[0].prompt
    assert len(result["decisions"]) == 1
    assert "later Tuesday decision supersedes" in result["decisions"][0]["text"]
    assert result["confirmation_items"] == []


def test_final_synthesis_uses_primary_budget_without_repair_reserve(tmp_path, monkeypatch):
    from asr_studio import notes_document

    monkeypatch.setattr(notes_document, "MAX_PRIMARY_PROMPT_BYTES", 20_000)
    snapshot = _snapshot()
    snapshot["segments"] = [
        {
            "id": f"{index:032x}",
            "start_seconds": float(index),
            "end_seconds": float(index + 1),
            "text": "source detail " + "x" * 16_000,
        }
        for index in range(1, 5)
    ]

    def respond(prompt, max_output_bytes):
        if "SOURCE_JSON:\n" in prompt:
            source = json.loads(prompt.split("SOURCE_JSON:\n", 1)[1])
            source_id = source["segments"][0]["id"]
            result = _valid_document()
            result["overview"] = [
                {"text": "chunk summary " + "x" * 7_000, "source_ids": [source_id]}
            ]
            output = json.dumps(result, separators=(",", ":"))
            assert len(output.encode("utf-8")) <= max_output_bytes
            return output

        payload = json.loads(prompt.split("SYNTHESIS_INPUT_JSON:\n", 1)[1])
        source_ids = list(
            dict.fromkeys(
                source_id
                for summary in payload["summaries"]
                for source_id in summary["source_order"]
            )
        )
        if "final meeting-level notes" not in prompt:
            result = _valid_document()
            result["overview"] = [{"text": "x" * 8_001, "source_ids": source_ids}]
            return json.dumps(result, separators=(",", ":"))

        result = _valid_document()
        result["overview"] = [
            {"text": "All four chunks were synthesized.", "source_ids": source_ids}
        ]
        return json.dumps(result, separators=(",", ":"))

    adapter = NotesOnlyAdapter(responder=respond)
    provider = _provider(tmp_path, adapter)

    result = provider.generate_notes_document(
        snapshot=snapshot,
        connection_version=7,
        model="gpt-6.1-sol",
        reasoning_effort="high",
    )

    synthesis_calls = [call for call in adapter.calls if "SYNTHESIS_INPUT_JSON:\n" in call.prompt]
    assert len(adapter.calls) == 5
    assert len(synthesis_calls) == 1
    assert "final meeting-level notes" in synthesis_calls[0].prompt
    assert synthesis_calls[0].max_output_bytes == 128_000
    assert result["overview"][0]["text"] == "All four chunks were synthesized."


def test_final_repair_is_skipped_when_actual_prompt_budget_cannot_fit_it(tmp_path, monkeypatch):
    from asr_studio import notes_document

    monkeypatch.setattr(notes_document, "MAX_PRIMARY_PROMPT_BYTES", 20_000)
    snapshot = _snapshot()
    snapshot["segments"] = [
        {
            "id": f"{index:032x}",
            "start_seconds": float(index),
            "end_seconds": float(index + 1),
            "text": "source detail " + "x" * 16_000,
        }
        for index in range(1, 5)
    ]

    def respond(prompt, _max_output_bytes):
        if "SOURCE_JSON:\n" in prompt:
            source = json.loads(prompt.split("SOURCE_JSON:\n", 1)[1])
            source_id = source["segments"][0]["id"]
            result = _valid_document()
            result["overview"] = [
                {"text": "chunk summary " + "x" * 7_000, "source_ids": [source_id]}
            ]
            return json.dumps(result, separators=(",", ":"))
        assert "final meeting-level notes" in prompt
        return "not-json" + "x" * (8_000 - len("not-json"))

    adapter = NotesOnlyAdapter(responder=respond)
    provider = _provider(tmp_path, adapter)

    with pytest.raises(ChatGPTError) as failed:
        provider.generate_notes_document(
            snapshot=snapshot,
            connection_version=7,
            model="gpt-6.1-sol",
            reasoning_effort="high",
        )

    assert failed.value.code == "notes_invalid_output"
    assert len(adapter.calls) == 5
    diagnostic = provider.store._read()["last_diagnostic"]
    assert diagnostic["stage"] == "final"
    assert diagnostic["attempt"] == "primary"
    assert diagnostic["provider_call_index"] == 5
    assert diagnostic["prompt_bytes"] == len(adapter.calls[-1].prompt.encode("utf-8"))
    assert diagnostic["response_limit"] == 128_000
    assert diagnostic["output_bytes"] == 8_000
    assert diagnostic["repair_state"] == "unavailable"


@pytest.mark.parametrize(
    "intermediate_bytes", [MAX_REDUCTION_OUTPUT_BYTES - 100, MAX_REDUCTION_OUTPUT_BYTES + 1]
)
def test_intermediate_reduction_has_its_own_hard_output_budget(
    tmp_path, monkeypatch, intermediate_bytes
):
    from asr_studio import notes_document

    monkeypatch.setattr(notes_document, "MAX_PRIMARY_PROMPT_BYTES", 20_000)
    snapshot = _snapshot()
    snapshot["segments"] = [
        {
            "id": f"{index:032x}",
            "start_seconds": float(index),
            "end_seconds": float(index + 1),
            "text": "source detail " + "x" * 16_000,
        }
        for index in range(1, 7)
    ]
    reduction_outputs: list[str] = []

    def respond(prompt, max_output_bytes):
        if "SOURCE_JSON:\n" in prompt:
            source = json.loads(prompt.split("SOURCE_JSON:\n", 1)[1])
            source_id = source["segments"][0]["id"]
            result = _valid_document()
            result["overview"] = [
                {"text": "chunk summary " + "x" * 7_300, "source_ids": [source_id]}
            ]
            output = json.dumps(result, separators=(",", ":"))
            assert len(output.encode("utf-8")) <= max_output_bytes
            return output

        payload = _payload(prompt, "SYNTHESIS_INPUT_JSON:\n")
        source_ids = list(
            dict.fromkeys(
                source_id
                for summary in payload["summaries"]
                for source_id in summary["source_order"]
            )
        )
        if "final meeting-level notes" in prompt:
            result = _valid_document()
            result["overview"] = [
                {"text": "All six chunks were synthesized.", "source_ids": source_ids}
            ]
            return json.dumps(result, separators=(",", ":"))

        assert max_output_bytes == MAX_REDUCTION_OUTPUT_BYTES
        result = _valid_document()
        marker = f"reduce-{len(reduction_outputs) + 1}|"
        result["overview"] = [{"text": marker, "source_ids": source_ids}]
        compact = json.dumps(result, separators=(",", ":"))
        target_text_bytes = intermediate_bytes - len(compact.encode("utf-8")) + len(marker)
        result["overview"][0]["text"] += "r" * (target_text_bytes - len(marker))
        output = json.dumps(result, separators=(",", ":"))
        assert len(output.encode("utf-8")) == intermediate_bytes
        reduction_outputs.append(output)
        return output

    adapter = NotesOnlyAdapter(responder=respond)
    provider = _provider(tmp_path, adapter)

    if intermediate_bytes > MAX_REDUCTION_OUTPUT_BYTES:
        with pytest.raises(ChatGPTError) as failed:
            provider.generate_notes_document(
                snapshot=snapshot,
                connection_version=7,
                model="gpt-6.1-sol",
                reasoning_effort="high",
            )

        assert failed.value.code == "notes_invalid_output"
        diagnostic = provider.store._read()["last_diagnostic"]
        # This provider keeps ignoring the cap, so the lower-target asks fail the same way.
        last_call = 7 + MAX_OUTPUT_RECOVERY_ATTEMPTS
        assert diagnostic["stage"] == "reduce"
        assert diagnostic["attempt"] == "recovery"
        assert diagnostic["provider_call_index"] == last_call
        assert diagnostic["response_limit"] == MAX_REDUCTION_OUTPUT_BYTES
        assert diagnostic["output_bytes"] == intermediate_bytes
        assert diagnostic["validation_reason"] == "size"
        assert diagnostic["repair_state"] == "unavailable"
        assert len(adapter.calls) == last_call
        return

    result = provider.generate_notes_document(
        snapshot=snapshot,
        connection_version=7,
        model="gpt-6.1-sol",
        reasoning_effort="high",
    )

    reduction_calls = [call for call in adapter.calls if "SYNTHESIS_INPUT_JSON:\n" in call.prompt]
    intermediate_calls = [
        call for call in reduction_calls if "final meeting-level notes" not in call.prompt
    ]
    assert len(intermediate_calls) == 4
    assert all(call.max_output_bytes == MAX_REDUCTION_OUTPUT_BYTES for call in intermediate_calls)
    assert all(len(output.encode("utf-8")) == intermediate_bytes for output in reduction_outputs)
    for call in reduction_calls:
        summaries = json.loads(call.prompt.split("SYNTHESIS_INPUT_JSON:\n", 1)[1])["summaries"]
        assert len(summaries) <= 2
        previous_source_end = -1
        for summary in summaries:
            source_order = list(summary["source_order"].values())
            assert source_order == sorted(source_order)
            if source_order:
                assert source_order[0] > previous_source_end
                previous_source_end = source_order[-1]
    final_payload = json.loads(reduction_calls[-1].prompt.split("SYNTHESIS_INPUT_JSON:\n", 1)[1])
    assert final_payload["summaries"][1]["notes"]["overview"][0]["text"].startswith("reduce-3|")
    assert result["overview"][0]["text"] == "All six chunks were synthesized."
    with provider.store.locked() as state:
        assert "last_diagnostic" not in state


def test_transport_failure_diagnostics_use_actual_stage_and_bounded_metadata(tmp_path, monkeypatch):
    from asr_studio import notes_document

    monkeypatch.setattr(notes_document, "MAX_PRIMARY_PROMPT_BYTES", 20_000)
    snapshot = _snapshot()
    snapshot["segments"] = [
        {
            "id": f"{index:032x}",
            "start_seconds": float(index),
            "end_seconds": float(index + 1),
            "text": "source detail " + "x" * 16_000,
        }
        for index in range(1, 7)
    ]

    def respond(prompt, _max_output_bytes):
        if "SOURCE_JSON:\n" in prompt:
            source = json.loads(prompt.split("SOURCE_JSON:\n", 1)[1])
            source_id = source["segments"][0]["id"]
            result = _valid_document()
            result["overview"] = [
                {"text": "chunk summary " + "x" * 7_300, "source_ids": [source_id]}
            ]
            return json.dumps(result, separators=(",", ":"))

        failure = ChatGPTError(
            502, "provider_output_too_large", "ChatGPT returned an oversized notes document."
        )
        failure.diagnostics = {
            "stage": "untrusted-stage",
            "attempt": "repair",
            "provider_call_index": 999,
            "prompt_bytes": -1,
            "response_limit": 1,
            "output_bytes": 16_001,
            "request_id": "req_reduce123",
            "private": "secret provider body",
        }
        raise failure

    adapter = NotesOnlyAdapter(responder=respond)
    provider = _provider(tmp_path, adapter)

    with pytest.raises(ChatGPTError) as failed:
        provider.generate_notes_document(
            snapshot=snapshot,
            connection_version=7,
            model="gpt-6.1-sol",
            reasoning_effort="high",
        )

    assert failed.value.code == "provider_output_too_large"
    diagnostic = provider.store._read()["last_diagnostic"]
    # The failure is the last lower-target ask: every ask is capped and diagnosed on its own.
    assert len(adapter.calls) == 7 + MAX_OUTPUT_RECOVERY_ATTEMPTS
    assert diagnostic["stage"] == "reduce"
    assert diagnostic["attempt"] == "recovery"
    assert diagnostic["provider_call_index"] == 7 + MAX_OUTPUT_RECOVERY_ATTEMPTS
    assert diagnostic["prompt_bytes"] == len(adapter.calls[-1].prompt.encode("utf-8"))
    assert diagnostic["response_limit"] == MAX_REDUCTION_OUTPUT_BYTES
    assert diagnostic["output_bytes"] == 16_001
    assert diagnostic["request_id"] == "req_reduce123"
    assert "private" not in json.dumps(diagnostic)
    assert "untrusted-stage" not in json.dumps(diagnostic)


def _many_segments_snapshot(count=600):
    snapshot = _snapshot()
    snapshot["segments"] = [
        {
            "id": f"{index + 1:032x}",
            "start_seconds": float(index),
            "end_seconds": float(index) + 0.5,
            "text": f"discussion detail {index} about the launch plan",
            "speaker_name": "Morgan",
        }
        for index in range(count)
    ]
    return snapshot


def _source_segments(prompt):
    return _payload(prompt, "SOURCE_JSON:\n")["segments"]


def _synthesis_summaries(prompt):
    return _payload(prompt, "SYNTHESIS_INPUT_JSON:\n")["summaries"]


def test_hourly_provider_maps_overlap_and_reduce_without_racing_state():
    from asr_studio.notes_pipeline import generate_notes_document

    snapshot = _snapshot()
    snapshot["segments"] = [
        {
            "id": f"{index:032x}",
            "start_seconds": start,
            "end_seconds": start + 1,
            "text": f"detail {index}",
            "speaker_name": "Morgan",
        }
        for index, start in enumerate((0.0, 1_200.0, 2_400.0, 3_600.0))
    ]
    active = 0
    max_active = 0
    state_lock = threading.Lock()
    completion_order = []
    final_payloads = []
    progress = []
    manifests = []

    def document(text, source_id):
        result = {
            "schema_version": 1,
            "overview": [{"text": text, "source_ids": [source_id]}],
            "key_topics": [],
            "decisions": [],
            "open_questions": [],
            "action_items": [],
            "confirmation_items": [],
        }
        return json.dumps(result)

    def transport(prompt, **kwargs):
        nonlocal active, max_active
        if kwargs["stage"] == "chunk":
            source = _payload(prompt, "SOURCE_JSON:\n")
            owned = [segment for segment in source["segments"] if not segment.get("overlap")]
            label = owned[0]["text"]
            with state_lock:
                active += 1
                max_active = max(max_active, active)
            try:
                if label == "detail 0":
                    time.sleep(0.08)
                completion_order.append(label)
                return document(label, owned[0]["id"])
            finally:
                with state_lock:
                    active -= 1
        final_payload = _payload(prompt, "SYNTHESIS_INPUT_JSON:\n")
        final_payloads.append(final_payload)
        source_id = next(iter(final_payload["summaries"][0]["source_order"]))
        return document("final", source_id)

    result = generate_notes_document(
        snapshot=snapshot,
        output_language="en",
        transport=transport,
        invalid_output_error=lambda diagnostics: AssertionError(diagnostics),
        limit_error=RuntimeError,
        on_coverage=manifests.append,
        on_progress=progress.append,
        provider="chatgpt",
        model="gpt-5.6-sol",
    )

    assert max_active == 2
    assert completion_order == ["detail 3", "detail 0"]
    assert result["overview"][0]["text"] == "final"
    assert len(final_payloads) == 1
    source_orders = [list(item["source_order"].values()) for item in final_payloads[0]["summaries"]]
    assert source_orders == sorted(source_orders)
    chunk_indices = [entry["provider_call_index"] for entry in manifests[0]["chunks"]]
    assert sorted(chunk_indices) == [1, 2]
    chunk_progress = [event["current"] for event in progress if event["stage"] == "chunking"]
    assert chunk_progress == sorted(chunk_progress)


def test_long_run_sends_budgeted_overlapping_requests_and_reports_coverage(tmp_path):
    from asr_studio.notes_document import MAX_CHUNK_OUTPUT_BYTES, prompt_fits_repair

    snapshot = _many_segments_snapshot()
    original = copy.deepcopy(snapshot)
    adapter = NotesOnlyAdapter(responder=lambda prompt, _limit: _document_for_prompt(prompt))
    provider = _provider(tmp_path, adapter)
    manifests = []

    provider.generate_notes_document(
        snapshot=snapshot,
        connection_version=7,
        model="gpt-6.1-sol",
        reasoning_effort="high",
        on_coverage=manifests.append,
    )

    chunk_calls = [call for call in adapter.calls if "SOURCE_JSON:\n" in call.prompt]
    assert len(chunk_calls) > 2
    previous_owned: list[str] | None = None
    for call in chunk_calls:
        # Prompt, citation schema, headroom and the repair reserve all fit the request budget.
        assert prompt_fits_repair(call.prompt, allowed_source_ids=call.allowed_source_ids)
        assert call.max_output_bytes == MAX_CHUNK_OUTPUT_BYTES
        segments = _source_segments(call.prompt)
        overlap = [item["id"] for item in segments if item.get("overlap")]
        owned = [item["id"] for item in segments if not item.get("overlap")]
        if previous_owned is None:
            assert overlap == []
        else:
            assert overlap and overlap == previous_owned[-len(overlap) :]
        assert call.allowed_source_ids == tuple(overlap + owned)  # overlap stays citable
        previous_owned = owned
    (manifest,) = manifests
    assert manifest["segment_count"] == len(snapshot["segments"])
    assert [chunk["provider_call_index"] for chunk in manifest["chunks"]] == list(
        range(1, len(chunk_calls) + 1)
    )
    assert manifest["chunks"][0]["owned"][0] == 0
    assert manifest["chunks"][-1]["owned"][1] == len(snapshot["segments"])
    assert snapshot == original


def test_coverage_records_the_repair_call_that_was_accepted(tmp_path):
    first = _valid_document()
    second = _valid_document()
    second["overview"][0]["source_ids"] = ["00001"]
    adapter = NotesOnlyAdapter(
        ["not-json", json.dumps(first), json.dumps(second), json.dumps(_valid_document())]
    )
    provider = _provider(tmp_path, adapter)
    manifests = []

    provider.generate_notes_document(
        snapshot=_chunked_snapshot(),
        connection_version=7,
        model="gpt-6.1-sol",
        reasoning_effort="high",
        on_coverage=manifests.append,
    )

    assert len(adapter.calls) == 4
    assert [chunk["provider_call_index"] for chunk in manifests[0]["chunks"]] == [2, 3]


def test_single_request_run_reports_a_manifest_for_its_one_call(tmp_path):
    adapter = NotesOnlyAdapter([json.dumps(_valid_document())])
    provider = _provider(tmp_path, adapter)
    manifests = []

    provider.generate_notes_document(
        snapshot=_snapshot(),
        connection_version=7,
        model="gpt-6.1-sol",
        reasoning_effort="high",
        on_coverage=manifests.append,
    )

    assert manifests == [
        {
            "version": 1,
            "segment_count": 1,
            "chunks": [
                {
                    "index": 1,
                    "owned": [0, 1],
                    "overlap": None,
                    "parts": [],
                    "provider_call_index": 1,
                }
            ],
            "provider_calls": 1,
            "output_recoveries": 0,
        }
    ]


def test_incomplete_coverage_stops_the_run_before_any_provider_call(tmp_path, monkeypatch):
    from asr_studio import notes_document

    real_plan_source = notes_document.plan_source

    def lossy_plan(snapshot, **kwargs):
        plan = real_plan_source(snapshot, **kwargs)
        return dataclasses.replace(plan, chunks=plan.chunks[:-1])

    monkeypatch.setattr(notes_document, "plan_source", lossy_plan)
    adapter = NotesOnlyAdapter(responder=lambda prompt, _limit: _document_for_prompt(prompt))
    provider = _provider(tmp_path, adapter)

    with pytest.raises(notes_document.NotesDocumentError, match="complete source coverage"):
        provider.generate_notes_document(
            snapshot=_chunked_snapshot(),
            connection_version=7,
            model="gpt-6.1-sol",
            reasoning_effort="high",
        )

    assert adapter.calls == []


def test_oversized_segment_is_sent_in_ordered_parts_and_cited_by_its_original_id(tmp_path):
    long_text = "budget review item " * 3_500
    snapshot = _snapshot()
    snapshot["segments"] = [
        {"id": "a" * 32, "start_seconds": 1.0, "end_seconds": 2.0, "text": "Opening remarks."},
        {"id": "b" * 32, "start_seconds": 3.25, "end_seconds": 99.5, "text": long_text},
        {"id": "c" * 32, "start_seconds": 100.0, "end_seconds": 101.0, "text": "Closing."},
    ]
    original = copy.deepcopy(snapshot)

    def respond(prompt, _limit):
        if "SOURCE_JSON:\n" in prompt:
            cited = _source_segments(prompt)[-1]["id"]
            document = _valid_document()
            document["overview"] = [{"text": "A concise summary.", "source_ids": [cited]}]
            return json.dumps(document)
        cited = list(
            dict.fromkeys(
                source_id
                for summary in _synthesis_summaries(prompt)
                for source_id in summary["source_order"]
            )
        )
        document = _valid_document()
        document["decisions"] = [{"text": "Everything was reviewed.", "source_ids": cited}]
        return json.dumps(document)

    adapter = NotesOnlyAdapter(responder=respond)
    provider = _provider(tmp_path, adapter)
    manifests = []

    result = provider.generate_notes_document(
        snapshot=snapshot,
        connection_version=7,
        model="gpt-6.1-sol",
        reasoning_effort="high",
        on_coverage=manifests.append,
    )

    part_entries = [
        item
        for call in adapter.calls
        if "SOURCE_JSON:\n" in call.prompt
        for item in _source_segments(call.prompt)
        if "part" in item
    ]
    count = len(part_entries)
    assert count >= 3
    assert [item["part"] for item in part_entries] == [[n, count] for n in range(1, count + 1)]
    assert len({item["id"] for item in part_entries}) == 1
    assert {(item["start_seconds"], item["end_seconds"]) for item in part_entries} == {(3.25, 99.5)}
    for call in adapter.calls:  # prompts carry aliases and no planner internals
        assert not any(original * 32 in call.prompt for original in "abc")
        assert '"bytes"' not in call.prompt
    # The manifest's byte ranges cover the whole stored text, in order.
    ranges = [part["bytes"] for chunk in manifests[0]["chunks"] for part in chunk["parts"]]
    assert ranges[0][0] == 0 and ranges[-1][1] == len(long_text.encode())
    assert all(right[0] <= left[1] for left, right in zip(ranges, ranges[1:], strict=False))
    assert "b" * 32 in {ref for item in result["decisions"] for ref in item["source_ids"]}
    assert snapshot == original


def test_overlap_repeats_are_dropped_before_reduction_but_distinct_items_survive(tmp_path):
    chunk_number = 0

    def respond(prompt, _limit):
        nonlocal chunk_number
        if "SYNTHESIS_INPUT_JSON:\n" in prompt:
            summaries = _synthesis_summaries(prompt)
            ids = list(dict.fromkeys(i for summary in summaries for i in summary["source_order"]))
            document = _valid_document()
            document["overview"] = [{"text": "Final.", "source_ids": ids[:1]}]
            return json.dumps(document)
        chunk_number += 1
        segments = _source_segments(prompt)
        overlap = [item["id"] for item in segments if item.get("overlap")]
        owned = [item["id"] for item in segments if not item.get("overlap")]
        document = _valid_document()
        document["overview"] = [{"text": "Topic.", "source_ids": owned[:1]}]
        document["open_questions"] = [
            {"text": f"Question {chunk_number}", "source_ids": [owned[-1]]}
        ]
        if overlap:
            # A rephrase of the previous chunk's closing question, resting only on the overlap.
            document["open_questions"].append(
                {"text": "Same question, rephrased", "source_ids": [overlap[-1]]}
            )
            # A point that needs both the overlap and this chunk is not a repeat.
            document["confirmation_items"] = [
                {"text": "Spans the boundary", "source_ids": [overlap[-1], owned[0]]}
            ]
        return json.dumps(document)

    adapter = NotesOnlyAdapter(responder=respond)
    provider = _provider(tmp_path, adapter)

    provider.generate_notes_document(
        snapshot=_many_segments_snapshot(300),
        connection_version=7,
        model="gpt-6.1-sol",
        reasoning_effort="high",
    )

    final_prompt = adapter.calls[-1].prompt
    summaries = [summary["notes"] for summary in _synthesis_summaries(final_prompt)]
    assert len(summaries) == chunk_number >= 2
    assert [item["text"] for item in summaries[0]["open_questions"]] == ["Question 1"]
    for number, notes in enumerate(summaries[1:], start=2):
        assert [item["text"] for item in notes["open_questions"]] == [
            f"Question {number}",
            "Same question, rephrased",
        ]
        assert [item["text"] for item in notes["confirmation_items"]] == ["Spans the boundary"]
    assert "overlap repeats" not in final_prompt  # the reducer is told, not given repeats
    assert "Neighbouring chunks overlap" in final_prompt


def _tight_budget(monkeypatch, *, limit=15_000):
    from asr_studio import notes_document

    monkeypatch.setattr(notes_document, "MAX_PROMPT_BYTES", limit)


def _four_chunk_snapshot():
    snapshot = _snapshot()
    snapshot["segments"] = [
        {
            "id": f"{index:032x}",
            "start_seconds": float(index),
            "end_seconds": float(index + 1),
            "text": f"source detail {index} " + "x" * 2_500,
        }
        for index in range(1, 5)
    ]
    return snapshot


def test_a_summary_that_cannot_pair_is_compacted_alone_and_the_tail_is_still_read(
    tmp_path, monkeypatch
):
    _tight_budget(monkeypatch)

    def respond(prompt, max_output_bytes):
        if "SOURCE_JSON:\n" in prompt:
            source_id = _source_segments(prompt)[0]["id"]
            document = _valid_document()
            document["overview"] = [{"text": "chunk " + "x" * 6_400, "source_ids": [source_id]}]
            return json.dumps(document, separators=(",", ":"))
        summaries = _synthesis_summaries(prompt)
        ids = list(dict.fromkeys(i for summary in summaries for i in summary["source_order"]))
        document = _valid_document()
        if "final meeting-level notes" in prompt:
            document["decisions"] = [{"text": "Read everything.", "source_ids": ids}]
        else:
            assert max_output_bytes == MAX_REDUCTION_OUTPUT_BYTES
            document["overview"] = [{"text": "compact " + "y" * 1_400, "source_ids": ids}]
        return json.dumps(document, separators=(",", ":"))

    adapter = NotesOnlyAdapter(responder=respond)
    provider = _provider(tmp_path, adapter)
    manifests = []

    result = provider.generate_notes_document(
        snapshot=_four_chunk_snapshot(),
        connection_version=7,
        model="gpt-6.1-sol",
        reasoning_effort="high",
        on_coverage=manifests.append,
    )

    reductions = [call for call in adapter.calls if "SYNTHESIS_INPUT_JSON:\n" in call.prompt]
    final = reductions[-1]
    assert "final meeting-level notes" in final.prompt
    inputs = [len(_synthesis_summaries(call.prompt)) for call in reductions[:-1]]
    # Level one cannot pair 6.5 KB summaries, so one of each pair is compacted alone; level two
    # then pairs the compacted summaries with their neighbours.
    assert inputs == [1, 1, 2, 2] and len(adapter.calls) == 9
    summaries = _synthesis_summaries(final.prompt)
    assert [list(item["source_order"]) for item in summaries] == [
        ["00000", "00001"],
        ["00002", "00003"],
    ]
    cited = {ref for item in result["decisions"] for ref in item["source_ids"]}
    assert cited == {f"{index:032x}" for index in range(1, 5)}  # the tail is in the notes
    assert len(manifests) == 1


def test_run_fails_instead_of_dropping_a_summary_that_does_not_shrink(tmp_path, monkeypatch):
    _tight_budget(monkeypatch)

    def respond(prompt, _limit):
        if "SOURCE_JSON:\n" in prompt:
            source_id = _source_segments(prompt)[0]["id"]
            document = _valid_document()
            document["overview"] = [{"text": "chunk " + "x" * 6_400, "source_ids": [source_id]}]
            return json.dumps(document, separators=(",", ":"))
        summaries = _synthesis_summaries(prompt)
        return json.dumps(summaries[0]["notes"], separators=(",", ":"))  # returned unchanged

    adapter = NotesOnlyAdapter(responder=respond)
    provider = _provider(tmp_path, adapter)
    manifests = []

    with pytest.raises(ChatGPTError) as failed:
        provider.generate_notes_document(
            snapshot=_four_chunk_snapshot(),
            connection_version=7,
            model="gpt-6.1-sol",
            reasoning_effort="high",
            on_coverage=manifests.append,
        )

    assert failed.value.code == "notes_synthesis_limit_exceeded"
    assert len(adapter.calls) == 5  # four chunks and the one compaction that did not shrink
    assert manifests == []


def test_repair_used_in_a_chunk_is_not_available_to_the_final_reduction(tmp_path):
    second = _valid_document()
    second["overview"][0]["source_ids"] = ["00001"]
    adapter = NotesOnlyAdapter(
        ["not-json", json.dumps(_valid_document()), json.dumps(second), "still-not-json"]
    )
    provider = _provider(tmp_path, adapter)
    manifests = []

    with pytest.raises(ChatGPTError) as invalid:
        provider.generate_notes_document(
            snapshot=_chunked_snapshot(),
            connection_version=7,
            model="gpt-6.1-sol",
            reasoning_effort="high",
            on_coverage=manifests.append,
        )

    assert invalid.value.code == "notes_invalid_output"
    assert len(adapter.calls) == 4
    assert sum("INVALID_OUTPUT:" in call.prompt for call in adapter.calls) == 1
    diagnostic = provider.store._read()["last_diagnostic"]
    assert diagnostic["stage"] == "final"
    assert diagnostic["provider_call_index"] == 4
    assert diagnostic["repair_state"] == "already_consumed"
    assert manifests == []


def test_network_failure_after_earlier_chunks_is_not_resent_and_reports_no_coverage(tmp_path):
    adapter = NotesOnlyAdapter(
        [
            json.dumps(_valid_document()),
            ChatGPTError(503, "provider_unavailable", "ChatGPT could not complete the notes."),
            json.dumps(_valid_document()),
        ]
    )
    provider = _provider(tmp_path, adapter)
    manifests = []

    with pytest.raises(ChatGPTError) as failed:
        provider.generate_notes_document(
            snapshot=_chunked_snapshot(),
            connection_version=7,
            model="gpt-6.1-sol",
            reasoning_effort="high",
            on_coverage=manifests.append,
        )

    assert failed.value.code == "provider_unavailable"
    # No resend, no repair and no later chunk: the operator starts a new run.
    assert len(adapter.calls) == 2
    assert len(adapter.replies) == 1
    assert manifests == []


def _aim(prompt):
    """The last output aim a prompt states: its reduction target or the one a recovery lowered."""
    return int(re.findall(r"aiming for (\d+) bytes or less", prompt)[-1])


def _lowered_aims(first, count):
    aims = []
    for _ in range(count):
        first = first * 3 // 4 // 100 * 100
        aims.append(first)
    return aims


def _within_request_budget(call):
    prompt_bytes = len(call.prompt.encode("utf-8"))
    schema_bytes = notes_schema_json_bytes(call.allowed_source_ids)
    return prompt_bytes <= MAX_PROMPT_BYTES and prompt_bytes + schema_bytes + 1_024 <= 35_000


def _too_large(output_bytes):
    """What the ChatGPT adapter raises for a response over its cap."""
    error = ChatGPTError(
        502, "provider_output_too_large", "ChatGPT returned an oversized notes document."
    )
    error.diagnostics = {"output_bytes": output_bytes}
    return error


def _generate(provider, snapshot, **options):
    return provider.generate_notes_document(
        snapshot=snapshot,
        connection_version=7,
        model="gpt-6.1-sol",
        reasoning_effort="high",
        **options,
    )


def _six_chunk_snapshot(monkeypatch):
    from asr_studio import notes_document

    monkeypatch.setattr(notes_document, "MAX_PRIMARY_PROMPT_BYTES", 20_000)
    snapshot = _snapshot()
    snapshot["segments"] = [
        {
            "id": f"{index:032x}",
            "start_seconds": float(index),
            "end_seconds": float(index + 1),
            "text": "source detail " + "x" * 16_000,
        }
        for index in range(1, 7)
    ]
    return snapshot


def _reduction_of_size(marker, source_ids, size):
    result = _valid_document()
    result["overview"] = [{"text": marker, "source_ids": source_ids}]
    padding = size - len(json.dumps(result, separators=(",", ":")).encode("utf-8"))
    result["overview"][0]["text"] += "r" * padding
    return json.dumps(result, separators=(",", ":"))


def _level_two_overflow(overshoot, overflows):
    """A provider whose first level-two reduce answers are over the cap, `overflows` times.

    Chunk summaries and level-one reductions fill the cap, so three of them cannot share a
    final prompt and a second reduction level is needed: 6 chunks, 3 + 1 reductions, 1 final.
    A lower-target prompt gets an answer that meets its stated aim.
    """
    pending = [overflows]
    answers = [0]

    def respond(prompt, max_output_bytes):
        if "SOURCE_JSON:\n" in prompt:
            source_id = _source_segments(prompt)[0]["id"]
            result = _valid_document()
            result["overview"] = [
                {"text": "chunk summary " + "x" * 7_300, "source_ids": [source_id]}
            ]
            return json.dumps(result, separators=(",", ":"))
        summaries = _synthesis_summaries(prompt)
        source_ids = list(
            dict.fromkeys(i for summary in summaries for i in summary["source_order"])
        )
        if "final meeting-level notes" in prompt:
            result = _valid_document()
            result["overview"] = [
                {"text": "All six chunks were synthesized.", "source_ids": source_ids}
            ]
            return json.dumps(result, separators=(",", ":"))
        level_two = any(
            summary["notes"]["overview"][0]["text"].startswith("reduce-") for summary in summaries
        )
        if level_two and pending[0]:
            pending[0] -= 1
            raise _too_large(max_output_bytes + overshoot)
        answers[0] += 1
        size = _aim(prompt) - 100 if RECOVERY_MARK in prompt else max_output_bytes - 100
        return _reduction_of_size(f"reduce-{answers[0]}|", source_ids, size)

    return respond


@pytest.mark.parametrize("overflows", [1, 2])
@pytest.mark.parametrize("overshoot", [1, 200])
def test_level_two_reduce_over_its_cap_is_asked_again_for_a_smaller_answer(
    tmp_path, monkeypatch, overshoot, overflows
):
    snapshot = _six_chunk_snapshot(monkeypatch)
    adapter = NotesOnlyAdapter(responder=_level_two_overflow(overshoot, overflows))
    provider = _provider(tmp_path, adapter)
    manifests = []

    result = _generate(provider, snapshot, on_coverage=manifests.append)

    assert result["overview"][0]["text"] == "All six chunks were synthesized."
    with provider.store.locked() as state:
        assert "last_diagnostic" not in state
    # Calls 1-6 are chunks, 7-9 level-one reductions and 10 the level-two reduction.
    primary, *asked_again = adapter.calls[9 : 10 + overflows]
    assert len(adapter.calls) == 11 + overflows
    assert RECOVERY_MARK not in primary.prompt
    assert _aim(primary.prompt) == MAX_REDUCTION_TARGET_BYTES
    assert (
        len(asked_again) == overflows <= MAX_OUTPUT_RECOVERY_ATTEMPTS <= MAX_OUTPUT_RECOVERY_CALLS
    )
    # The same request again: only a lower aim is added, so the targets strictly decrease.
    assert all(call.prompt.startswith(primary.prompt) for call in asked_again)
    assert [_aim(call.prompt) for call in asked_again] == _lowered_aims(
        MAX_REDUCTION_TARGET_BYTES, overflows
    )
    assert all(_aim(call.prompt) < MAX_REDUCTION_TARGET_BYTES for call in asked_again)
    # The hard cap and the prompt budget are the same for every ask.
    assert all(
        call.max_output_bytes == MAX_REDUCTION_OUTPUT_BYTES for call in [primary, *asked_again]
    )
    assert all(_within_request_budget(call) for call in adapter.calls)
    (manifest,) = manifests
    assert manifest["provider_calls"] == len(adapter.calls)
    assert manifest["output_recoveries"] == overflows


def test_chunk_over_its_cap_is_asked_again_for_a_smaller_answer(tmp_path):
    from asr_studio.notes_document import MAX_CHUNK_OUTPUT_BYTES

    second = _valid_document()
    second["overview"][0]["source_ids"] = ["00001"]
    adapter = NotesOnlyAdapter(
        [
            _too_large(MAX_CHUNK_OUTPUT_BYTES + 1),
            json.dumps(_valid_document()),
            json.dumps(second),
            json.dumps(_valid_document()),
        ]
    )
    provider = _provider(tmp_path, adapter)
    manifests = []

    result = _generate(provider, _chunked_snapshot(), on_coverage=manifests.append)

    assert result["overview"]
    primary, asked_again = adapter.calls[:2]
    assert RECOVERY_MARK not in primary.prompt and RECOVERY_MARK in asked_again.prompt
    assert asked_again.prompt.startswith(primary.prompt)
    assert _aim(asked_again.prompt) == _lowered_aims(MAX_CHUNK_OUTPUT_BYTES, 1)[0]
    assert primary.max_output_bytes == asked_again.max_output_bytes == MAX_CHUNK_OUTPUT_BYTES
    assert all(_within_request_budget(call) for call in adapter.calls)
    (manifest,) = manifests
    assert [chunk["provider_call_index"] for chunk in manifest["chunks"]] == [2, 3]
    assert (manifest["provider_calls"], manifest["output_recoveries"]) == (4, 1)


def test_vietnamese_run_asks_again_in_vietnamese(tmp_path):
    adapter = NotesOnlyAdapter([_too_large(MAX_OUTPUT_BYTES + 1), json.dumps(_valid_document())])
    provider = _provider(tmp_path, adapter)

    _generate(provider, _snapshot(), output_language="vi")

    asked_again = adapter.calls[1].prompt
    aim = _lowered_aims(MAX_OUTPUT_BYTES, 1)[0]
    assert f"hướng tới tối đa {aim} byte" in asked_again
    assert RECOVERY_MARK not in asked_again


def test_chunk_that_stays_over_its_cap_fails_with_the_overflow_and_its_diagnostics(tmp_path):
    from asr_studio.notes_document import MAX_CHUNK_OUTPUT_BYTES

    adapter = NotesOnlyAdapter(
        [_too_large(MAX_CHUNK_OUTPUT_BYTES + 5)] * (1 + MAX_OUTPUT_RECOVERY_ATTEMPTS)
    )
    provider = _provider(tmp_path, adapter)
    manifests = []

    with pytest.raises(ChatGPTError) as failed:
        _generate(provider, _chunked_snapshot(), on_coverage=manifests.append)

    assert failed.value.code == "provider_output_too_large"
    assert len(adapter.calls) == 1 + MAX_OUTPUT_RECOVERY_ATTEMPTS
    assert sum(RECOVERY_MARK in call.prompt for call in adapter.calls) == (
        MAX_OUTPUT_RECOVERY_ATTEMPTS
    )
    diagnostic = provider.store._read()["last_diagnostic"]
    assert diagnostic["code"] == "provider_output_too_large"
    assert diagnostic["stage"] == "chunk"
    assert diagnostic["attempt"] == "recovery"
    assert diagnostic["provider_call_index"] == len(adapter.calls)
    assert diagnostic["prompt_bytes"] == len(adapter.calls[-1].prompt.encode("utf-8"))
    assert diagnostic["response_limit"] == MAX_CHUNK_OUTPUT_BYTES
    assert diagnostic["output_bytes"] == MAX_CHUNK_OUTPUT_BYTES + 5
    # The error that reaches the caller is the last overflow, with the diagnostics it recorded.
    recorded = {key: value for key, value in diagnostic.items() if key not in {"code", "at"}}
    assert failed.value.diagnostics == recorded
    assert manifests == []  # no partial run is reported, so none is published


def test_level_two_reduce_that_stays_over_its_cap_fails_with_the_overflow_and_its_diagnostics(
    tmp_path, monkeypatch
):
    overflows = 1 + MAX_OUTPUT_RECOVERY_ATTEMPTS
    adapter = NotesOnlyAdapter(responder=_level_two_overflow(5, overflows))
    provider = _provider(tmp_path, adapter)
    manifests = []

    with pytest.raises(ChatGPTError) as failed:
        _generate(provider, _six_chunk_snapshot(monkeypatch), on_coverage=manifests.append)

    assert failed.value.code == "provider_output_too_large"
    assert len(adapter.calls) == 9 + overflows  # nothing is sent after the last allowed ask
    diagnostic = provider.store._read()["last_diagnostic"]
    assert diagnostic["code"] == "provider_output_too_large"
    assert diagnostic["stage"] == "reduce"
    assert diagnostic["attempt"] == "recovery"
    assert diagnostic["provider_call_index"] == 9 + overflows
    assert diagnostic["prompt_bytes"] == len(adapter.calls[-1].prompt.encode("utf-8"))
    assert diagnostic["response_limit"] == MAX_REDUCTION_OUTPUT_BYTES
    assert diagnostic["output_bytes"] == MAX_REDUCTION_OUTPUT_BYTES + 5
    recorded = {key: value for key, value in diagnostic.items() if key not in {"code", "at"}}
    assert failed.value.diagnostics == recorded
    assert manifests == []


def test_recoveries_are_capped_across_the_whole_task(tmp_path, monkeypatch):
    _tight_budget(monkeypatch)

    def respond(prompt, max_output_bytes):
        # The first ask of every request is too big. A chunk gets a fitting answer when asked
        # again, but a reduction stays too big however often it is asked: only the cap stops it.
        if RECOVERY_MARK not in prompt or "SOURCE_JSON:\n" not in prompt:
            raise _too_large(max_output_bytes + 5)
        source_id = _source_segments(prompt)[0]["id"]
        document = _valid_document()
        document["overview"] = [{"text": "chunk " + "x" * 6_400, "source_ids": [source_id]}]
        return json.dumps(document, separators=(",", ":"))

    adapter = NotesOnlyAdapter(responder=respond)
    provider = _provider(tmp_path, adapter)
    manifests = []

    with pytest.raises(ChatGPTError) as failed:
        _generate(provider, _four_chunk_snapshot(), on_coverage=manifests.append)

    # Each of the four chunks used one ask again; the first reduction finds none left.
    assert failed.value.code == "provider_output_too_large"
    assert sum(RECOVERY_MARK in call.prompt for call in adapter.calls) == MAX_OUTPUT_RECOVERY_CALLS
    assert len(adapter.calls) == 2 * MAX_OUTPUT_RECOVERY_CALLS + 1
    diagnostic = provider.store._read()["last_diagnostic"]
    assert (diagnostic["stage"], diagnostic["attempt"]) == ("reduce", "primary")
    assert diagnostic["provider_call_index"] == len(adapter.calls)
    assert manifests == []


def test_a_lower_target_prompt_that_does_not_fit_is_not_sent(tmp_path, monkeypatch):
    from asr_studio import notes_document

    def respond(prompt, max_output_bytes):
        # The request now fills its whole budget, so the added lower aim has no room.
        monkeypatch.setattr(notes_document, "MAX_PROMPT_BYTES", len(prompt.encode("utf-8")))
        raise _too_large(max_output_bytes + 1)

    adapter = NotesOnlyAdapter(responder=respond)
    provider = _provider(tmp_path, adapter)

    with pytest.raises(ChatGPTError) as failed:
        _generate(provider, _snapshot())

    assert failed.value.code == "provider_output_too_large"
    assert len(adapter.calls) == 1
    diagnostic = provider.store._read()["last_diagnostic"]
    assert (diagnostic["stage"], diagnostic["attempt"]) == ("single", "primary")
    assert diagnostic["provider_call_index"] == 1


def test_provider_failure_while_asking_again_is_not_retried(tmp_path):
    adapter = NotesOnlyAdapter(
        [
            _too_large(MAX_OUTPUT_BYTES + 1),
            ChatGPTError(503, "provider_unavailable", "ChatGPT could not complete the notes."),
            json.dumps(_valid_document()),
        ]
    )
    provider = _provider(tmp_path, adapter)
    manifests = []

    with pytest.raises(ChatGPTError) as failed:
        _generate(provider, _snapshot(), on_coverage=manifests.append)

    assert failed.value.code == "provider_unavailable"
    assert len(adapter.calls) == 2 and len(adapter.replies) == 1
    assert manifests == []


def test_a_smaller_answer_that_fails_validation_gets_the_one_repair(tmp_path):
    adapter = NotesOnlyAdapter(
        [_too_large(MAX_OUTPUT_BYTES + 1), "not-json", json.dumps(_valid_document())]
    )
    provider = _provider(tmp_path, adapter)
    manifests = []

    result = _generate(provider, _snapshot(), on_coverage=manifests.append)

    assert result["overview"][0]["text"] == "Morgan will prepare a draft."
    _, asked_again, repair = adapter.calls
    assert RECOVERY_MARK in asked_again.prompt and RECOVERY_MARK not in repair.prompt
    assert "INVALID_OUTPUT:\nnot-json" in repair.prompt
    (manifest,) = manifests
    assert manifest["chunks"][0]["provider_call_index"] == 3
    assert (manifest["provider_calls"], manifest["output_recoveries"]) == (3, 1)


def test_a_chunk_over_its_cap_is_asked_again_after_the_task_repair_was_spent(tmp_path):
    from asr_studio.notes_document import MAX_CHUNK_OUTPUT_BYTES

    second = _valid_document()
    second["overview"][0]["source_ids"] = ["00001"]
    adapter = NotesOnlyAdapter(
        [
            "not-json",
            json.dumps(_valid_document()),
            _too_large(MAX_CHUNK_OUTPUT_BYTES + 1),
            json.dumps(second),
            json.dumps(_valid_document()),
        ]
    )
    provider = _provider(tmp_path, adapter)
    manifests = []

    result = _generate(provider, _chunked_snapshot(), on_coverage=manifests.append)

    assert result["overview"]
    # Chunk 1 and its repair, chunk 2 over its cap and asked again, then the final reduction.
    _, repair, primary, asked_again, _ = adapter.calls
    assert "INVALID_OUTPUT:" in repair.prompt and RECOVERY_MARK not in repair.prompt
    assert RECOVERY_MARK not in primary.prompt and RECOVERY_MARK in asked_again.prompt
    assert asked_again.prompt.startswith(primary.prompt)
    (manifest,) = manifests
    assert [chunk["provider_call_index"] for chunk in manifest["chunks"]] == [2, 4]
    assert (manifest["provider_calls"], manifest["output_recoveries"]) == (5, 1)


def test_final_synthesis_over_its_cap_is_asked_again_for_a_smaller_answer(tmp_path):
    second = _valid_document()
    second["overview"][0]["source_ids"] = ["00001"]
    adapter = NotesOnlyAdapter(
        [
            json.dumps(_valid_document()),
            json.dumps(second),
            _too_large(MAX_OUTPUT_BYTES + 1),
            json.dumps(_valid_document()),
        ]
    )
    provider = _provider(tmp_path, adapter)
    manifests = []

    result = _generate(provider, _chunked_snapshot(), on_coverage=manifests.append)

    assert result["overview"]
    final, asked_again = adapter.calls[2:]
    assert "final meeting-level notes" in final.prompt
    assert RECOVERY_MARK not in final.prompt and RECOVERY_MARK in asked_again.prompt
    assert asked_again.prompt.startswith(final.prompt)
    assert _aim(asked_again.prompt) == 96_000  # three quarters of the 128000 byte cap
    assert final.max_output_bytes == asked_again.max_output_bytes == MAX_OUTPUT_BYTES
    (manifest,) = manifests
    assert (manifest["provider_calls"], manifest["output_recoveries"]) == (4, 1)


def test_a_lower_target_prompt_that_overruns_the_context_for_its_ids_is_not_sent(
    tmp_path, monkeypatch
):
    from asr_studio import notes_document

    scope = ("00000",)
    assert notes_schema_json_bytes(scope) > notes_schema_json_bytes(())  # the ids widen the schema

    def respond(prompt, max_output_bytes):
        if len(adapter.calls) == 1:
            # The lower aim fits the prompt cap but is one byte too many for the context left
            # once the schema of this request's ids is counted: a check without the ids lets it go.
            lowered = notes_document.build_recovery_prompt(
                prompt, target_bytes=_lowered_aims(MAX_OUTPUT_BYTES, 1)[0], allowed_source_ids=scope
            )
            monkeypatch.setattr(
                notes_document,
                "_CONTEXT_INPUT_BUDGET_BYTES",
                len(lowered.encode("utf-8"))
                + notes_schema_json_bytes(scope)
                + notes_document._CONTEXT_INPUT_HEADROOM_BYTES
                - 1,
            )
        raise _too_large(max_output_bytes + 1)

    adapter = NotesOnlyAdapter(responder=respond)
    provider = _provider(tmp_path, adapter)

    with pytest.raises(ChatGPTError) as failed:
        _generate(provider, _snapshot())

    assert failed.value.code == "provider_output_too_large"
    assert len(adapter.calls) == 1
    diagnostic = provider.store._read()["last_diagnostic"]
    assert (diagnostic["stage"], diagnostic["attempt"]) == ("single", "primary")


def test_parallel_map_progress_reports_completed_results_before_all_maps_finish():
    from asr_studio.notes_pipeline import generate_notes_document

    snapshot = _snapshot()
    snapshot["segments"] = [
        {
            "id": f"{index:032x}",
            "start_seconds": start,
            "end_seconds": start + 1,
            "text": label,
            "speaker_name": "Morgan",
        }
        for index, start, label in ((1, 0.0, "first"), (2, 3_601.0, "second"))
    ]
    entered = threading.Barrier(2)
    second_returned = threading.Event()
    intermediate_progress = threading.Event()
    initial_counts = []
    progress = []

    def on_progress(event):
        progress.append(event)
        if event["stage"] == "chunking" and event["current"] == 1 and second_returned.is_set():
            intermediate_progress.set()

    def transport(prompt, **kwargs):
        if kwargs["stage"] == "chunk":
            initial_counts.append([x["current"] for x in progress if x["stage"] == "chunking"][-1])
            entered.wait(timeout=5)
            payload = _payload(prompt, "SOURCE_JSON:\n")
            owned = [x for x in payload["segments"] if not x.get("overlap")]
            if owned[0]["text"] == "first":
                assert intermediate_progress.wait(timeout=5), (
                    "First completed map was not reported while another map was running"
                )
            else:
                second_returned.set()
        result = _valid_document()
        result["overview"] = [{"text": "Purpose", "source_ids": [kwargs["allowed_source_ids"][0]]}]
        return json.dumps(result)

    generate_notes_document(
        snapshot=snapshot,
        transport=transport,
        output_language="en",
        invalid_output_error=lambda value: AssertionError(value),
        limit_error=RuntimeError,
        provider="chatgpt",
        model="gpt-5.6-sol",
        on_progress=on_progress,
    )

    assert initial_counts == [0, 0]
    assert [x["current"] for x in progress if x["stage"] == "chunking"] == [0, 1, 2]


def test_source_audit_invalid_output_retains_stage_and_redacts_private_source_ids(tmp_path):
    invalid_document = _valid_document()
    invalid_document["overview"][0]["source_ids"] = ["private-unknown-source-id"]
    bad = json.dumps(invalid_document)
    adapter = NotesOnlyAdapter([json.dumps(_valid_document()), bad, bad])
    provider = _provider(tmp_path, adapter)
    with pytest.raises(ChatGPTError) as failure:
        provider.generate_notes_document(
            snapshot=_snapshot(),
            connection_version=7,
            model="gpt-5.6-sol",
            reasoning_effort="medium",
        )
    assert failure.value.code == "notes_invalid_output"
    assert len(adapter.calls) == 3
    diagnostic = provider.store._read()["last_diagnostic"]
    assert diagnostic["stage"] == "audit"
    assert diagnostic["provider_call_index"] == 3
    assert diagnostic["attempt"] == "repair"
    assert "private-unknown-source-id" not in json.dumps(diagnostic)
