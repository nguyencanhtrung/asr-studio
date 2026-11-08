"""Notes generation through either provider, chosen per run, over the shared pipeline."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import threading
import time
import uuid
from contextlib import asynccontextmanager, suppress
from itertools import pairwise

import pytest
from fake_chatgpt import FakeChatGPTAdapter
from httpx import ASGITransport, AsyncClient
from notes_helpers import seed_completed_transcript
from test_notes_provider import (
    RECOVERY_MARK,
    NotesOnlyAdapter,
    _aim,
    _chunked_snapshot,
    _document_for_prompt,
    _lowered_aims,
    _provider,
    _snapshot,
    _valid_document,
)

from asr_studio import notes_runner, worker
from asr_studio.antigravity import (
    AntigravityError,
    AntigravityProvider,
    OmpRpcAdapter,
    _strip_json_fence,
)
from asr_studio.app import create_app
from asr_studio.chatgpt import DIRECT_SCOPE, ChatGPTError, ChatGPTProvider
from asr_studio.config import Settings
from asr_studio.notes import (
    accept_notes_task,
    get_note,
    last_notes_provider,
    notes_state,
    publish_notes_revision,
    update_note,
)
from asr_studio.notes_document import (
    MAX_CHUNK_OUTPUT_BYTES,
    MAX_OUTPUT_BYTES,
    MAX_OUTPUT_RECOVERY_ATTEMPTS,
    render_markdown,
)
from asr_studio.storage import database_path
from asr_studio.tasks import TaskError, cancel_task, claim_next_task, fail_task
from asr_studio.worker import acquire_worker_lock

PINNED_MODEL = "gemini-3-pro-high"
INSTRUCTION = "\n\nReturn only one JSON object"


def _original_prompt(prompt: str) -> str:
    return prompt.split(INSTRUCTION, 1)[0]


def _answer(prompt: str) -> str:
    original = _original_prompt(prompt).split("\nRepair the following invalid output", 1)[0]
    return _document_for_prompt(original)


class FakeOmp:
    """Adapter-level double: one call is one fresh OMP conversation."""

    def __init__(self, responder=_answer, replies=None):
        self.responder = responder
        self.replies = list(replies or [])
        self.calls: list[tuple[str, str]] = []
        self.levels: list[str] = []
        self.catalog_calls = 0
        self.verify_calls = 0

    def login(self, interaction):
        raise AssertionError("Notes never start a login.")

    def list_models(self):
        self.catalog_calls += 1
        return []

    def verify(self, model, thinking_level="high"):
        self.verify_calls += 1
        return {"ok": True}

    def logout(self):
        return True

    def generate(self, model, prompt, thinking_level="high"):
        self.calls.append((model, prompt))
        self.levels.append(thinking_level)
        if self.replies:
            reply = self.replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply
        return self.responder(prompt)


def _antigravity(tmp_path, adapter, *, status="connected", version=4, model=PINNED_MODEL):
    provider = AntigravityProvider(tmp_path / "provider", adapter=adapter)
    provider.ensure()
    with provider.store.locked(write=True) as state:
        state["connection_status"] = status
        state["connection_version"] = version
        state["selected_model"] = model
    return provider


def _generate(provider, snapshot, *, version=4, model=PINNED_MODEL, **options):
    return provider.generate_notes_document(
        snapshot=snapshot,
        connection_version=version,
        model=model,
        reasoning_effort="default",
        **options,
    )


def test_antigravity_notes_use_the_shared_pipeline_for_single_and_chunked_sources(tmp_path):
    adapter = FakeOmp()
    provider = _antigravity(tmp_path, adapter)

    single = _generate(provider, _snapshot())

    assert len(adapter.calls) == 1
    model, prompt = adapter.calls[0]
    assert model == PINNED_MODEL
    # Sources are projected to short aliases for the call and restored afterwards.
    assert "c" * 32 not in prompt
    schema = json.loads(prompt.split("allows:\n", 1)[1])
    assert schema["$defs"]["source_id"]["enum"] == ["00000"]
    assert single["overview"][0]["source_ids"] == ["c" * 32]
    assert render_markdown(single, _snapshot(), output_language="en")

    adapter.calls.clear()
    chunked = _generate(provider, _chunked_snapshot())
    assert len(adapter.calls) == 3
    assert {model for model, _ in adapter.calls} == {PINNED_MODEL}
    assert chunked["overview"]
    assert all(source_id == f"{1:032x}" for source_id in chunked["overview"][0]["source_ids"])


def test_antigravity_accepts_one_wrapping_json_fence_only(tmp_path):
    document = json.dumps(_valid_document())
    assert _strip_json_fence(f"```json\n{document}\n```\n") == document
    assert _strip_json_fence(f"```\n{document}\n```") == document
    for unchanged in (f"Here it is:\n```json\n{document}\n```", f"{document}\n```", "not json"):
        assert _strip_json_fence(unchanged) == unchanged

    adapter = FakeOmp(replies=[f"```json\n{document}\n```"])
    result = _generate(_antigravity(tmp_path, adapter), _snapshot())
    assert result["overview"][0]["text"] == "Morgan will prepare a draft."
    assert len(adapter.calls) == 1

    strict = FakeOmp(replies=[f"Here it is:\n```json\n{document}\n```", "still not json"])
    with pytest.raises(AntigravityError) as failed:
        _generate(_antigravity(tmp_path / "strict", strict), _snapshot())
    assert failed.value.code == "notes_invalid_output"
    assert len(strict.calls) == 2


def test_antigravity_invalid_output_gets_one_repair_per_task_then_fails(tmp_path):
    adapter = FakeOmp(replies=["not json", "still not json"])
    with pytest.raises(AntigravityError) as failed:
        _generate(_antigravity(tmp_path, adapter), _snapshot())
    assert failed.value.code == "notes_invalid_output"
    assert failed.value.status_code == 502
    assert "Antigravity" in failed.value.public_message
    assert "ChatGPT" not in failed.value.public_message
    assert len(adapter.calls) == 2
    assert "INVALID_OUTPUT:" in adapter.calls[1][1]

    # The single repair budget is per task, not per chunk.
    plan = iter(["bad", "ok", "bad"])

    def respond(prompt):
        return "bad" if next(plan) == "bad" else _answer(prompt)

    chunked = FakeOmp(responder=respond)
    with pytest.raises(AntigravityError) as exhausted:
        _generate(_antigravity(tmp_path / "chunked", chunked), _chunked_snapshot())
    assert exhausted.value.code == "notes_invalid_output"
    assert len(chunked.calls) == 3


def _oversized(cap):
    """A well-formed document just over a cap, as OMP can return since it ignores the cap."""
    document = _valid_document()
    document["overview"][0]["text"] = "x" * cap
    return json.dumps(document)


def test_antigravity_oversized_document_is_asked_again_for_a_smaller_one(tmp_path):
    adapter = FakeOmp(replies=[_oversized(MAX_OUTPUT_BYTES), json.dumps(_valid_document())])

    result = _generate(_antigravity(tmp_path, adapter), _snapshot())

    assert result["overview"][0]["text"] == "Morgan will prepare a draft."
    first, second = (_original_prompt(prompt) for _, prompt in adapter.calls)
    assert RECOVERY_MARK not in first and RECOVERY_MARK in second
    assert second.startswith(first)
    assert _aim(second) == _lowered_aims(MAX_OUTPUT_BYTES, 1)[0] < MAX_OUTPUT_BYTES
    assert "INVALID_OUTPUT:" not in second  # the oversized output is not sent back to repair


def test_antigravity_oversized_chunk_recovers_and_the_manifest_counts_it(tmp_path):
    adapter = FakeOmp(replies=[_oversized(MAX_CHUNK_OUTPUT_BYTES)])
    manifests = []

    result = _generate(
        _antigravity(tmp_path, adapter), _chunked_snapshot(), on_coverage=manifests.append
    )

    assert result["overview"]
    prompts = [_original_prompt(prompt) for _, prompt in adapter.calls]
    assert len(prompts) == 4  # chunk 1 twice, chunk 2, then the final reduction
    assert RECOVERY_MARK in prompts[1] and sum(RECOVERY_MARK in item for item in prompts) == 1
    assert prompts[1].startswith(prompts[0])
    (manifest,) = manifests
    assert [chunk["provider_call_index"] for chunk in manifest["chunks"]] == [2, 3]
    assert (manifest["provider_calls"], manifest["output_recoveries"]) == (4, 1)


def test_antigravity_oversized_chunk_is_asked_again_after_the_task_repair_was_spent(tmp_path):
    adapter = FakeOmp(
        replies=["not json", json.dumps(_valid_document()), _oversized(MAX_CHUNK_OUTPUT_BYTES)]
    )
    manifests = []

    result = _generate(
        _antigravity(tmp_path, adapter), _chunked_snapshot(), on_coverage=manifests.append
    )

    assert result["overview"]
    # Chunk 1 and its repair, chunk 2 over its cap and asked again, then the final reduction.
    prompts = [_original_prompt(prompt) for _, prompt in adapter.calls]
    assert len(prompts) == 5
    assert "INVALID_OUTPUT:" in prompts[1] and RECOVERY_MARK not in prompts[1]
    assert RECOVERY_MARK not in prompts[2] and RECOVERY_MARK in prompts[3]
    assert prompts[3].startswith(prompts[2]) and sum(RECOVERY_MARK in p for p in prompts) == 1
    (manifest,) = manifests
    assert [chunk["provider_call_index"] for chunk in manifest["chunks"]] == [2, 4]
    assert (manifest["provider_calls"], manifest["output_recoveries"]) == (5, 1)


def test_antigravity_document_that_stays_oversized_fails_as_before(tmp_path):
    adapter = FakeOmp(replies=[_oversized(MAX_OUTPUT_BYTES)] * (1 + MAX_OUTPUT_RECOVERY_ATTEMPTS))

    with pytest.raises(AntigravityError) as failed:
        _generate(_antigravity(tmp_path, adapter), _snapshot())

    assert (failed.value.status_code, failed.value.code) == (502, "notes_invalid_output")
    assert len(adapter.calls) == 1 + MAX_OUTPUT_RECOVERY_ATTEMPTS
    assert not any("INVALID_OUTPUT:" in prompt for _, prompt in adapter.calls)


def test_antigravity_other_failures_keep_their_paths_beside_the_recovery(tmp_path):
    # Another validation failure is repaired as before, with no lower aim.
    adapter = FakeOmp(replies=["not json", json.dumps(_valid_document())])
    _generate(_antigravity(tmp_path / "repair", adapter), _snapshot())
    assert len(adapter.calls) == 2
    assert "INVALID_OUTPUT:" in adapter.calls[1][1]
    assert not any(RECOVERY_MARK in prompt for _, prompt in adapter.calls)

    # A smaller answer with another flaw gets the task's one repair.
    adapter = FakeOmp(
        replies=[_oversized(MAX_OUTPUT_BYTES), "not json", json.dumps(_valid_document())]
    )
    _generate(_antigravity(tmp_path / "flawed", adapter), _snapshot())
    prompts = [prompt for _, prompt in adapter.calls]
    assert [RECOVERY_MARK in prompt for prompt in prompts] == [False, True, False]
    assert "INVALID_OUTPUT:" in prompts[2]

    # A provider failure stops at once, also while asking again.
    adapter = FakeOmp(
        replies=[
            _oversized(MAX_OUTPUT_BYTES),
            AntigravityError(503, "provider_unavailable", "OMP is unavailable."),
            json.dumps(_valid_document()),
        ]
    )
    with pytest.raises(AntigravityError) as unavailable:
        _generate(_antigravity(tmp_path / "unavailable", adapter), _snapshot())
    assert unavailable.value.code == "provider_unavailable"
    assert len(adapter.calls) == 2 and len(adapter.replies) == 1


@pytest.mark.parametrize(
    ("error", "expected_status", "version_after"),
    [
        (AntigravityError(429, "provider_quota", "quota"), "connected", 4),
        (AntigravityError(422, "model_unavailable", "model"), "connected", 4),
        (AntigravityError(409, "reconnect_required", "login"), "reconnect_required", 5),
        (AntigravityError(503, "provider_timeout", "slow"), "connected", 4),
    ],
)
def test_antigravity_provider_errors_are_not_retried_or_replaced(
    tmp_path, error, expected_status, version_after
):
    adapter = FakeOmp(replies=[error])
    provider = _antigravity(tmp_path, adapter)

    with pytest.raises(AntigravityError) as failed:
        _generate(provider, _snapshot())

    assert failed.value.code == error.code
    assert len(adapter.calls) == 1
    view = provider.provider_view()
    assert (view["status"], view["connection_version"]) == (expected_status, version_after)
    with provider.store.locked() as state:
        assert state["selected_model"] == PINNED_MODEL


def test_antigravity_pin_requires_a_connection_and_a_saved_model(tmp_path):
    provider = _antigravity(tmp_path, FakeOmp(), model=None)
    with pytest.raises(AntigravityError) as unselected:
        provider.notes_task_pin()
    assert (unselected.value.status_code, unselected.value.code) == (409, "notes_model_unselected")
    assert "model" in unselected.value.public_message

    provider.save_notes_preference(PINNED_MODEL)
    assert provider.notes_task_pin() == {
        "provider": "antigravity",
        "connection_version": 4,
        "model": PINNED_MODEL,
        "reasoning_effort": "high",
    }

    for status, code in (
        ("disconnected", "provider_disconnected"),
        ("reconnect_required", "reconnect_required"),
    ):
        with provider.store.locked(write=True) as state:
            state["connection_status"] = status
        with pytest.raises(AntigravityError) as refused:
            provider.notes_task_pin()
        assert (refused.value.status_code, refused.value.code) == (409, code)


def test_antigravity_run_keeps_the_pinned_model_but_stops_when_the_connection_changes(tmp_path):
    def switch_preference_during_the_run(prompt):
        provider.save_notes_preference("gemini-other")
        return _answer(prompt)

    adapter = FakeOmp(responder=switch_preference_during_the_run)
    provider = _antigravity(tmp_path, adapter)

    _generate(provider, _chunked_snapshot())

    assert [model for model, _ in adapter.calls] == [PINNED_MODEL] * 3
    assert provider.saved_notes_model() == "gemini-other"

    adapter.calls.clear()
    with provider.store.locked(write=True) as state:
        state["connection_version"] = 5
    with pytest.raises(AntigravityError) as changed:
        _generate(provider, _snapshot())
    assert (changed.value.status_code, changed.value.code) == (409, "connection_changed")
    assert adapter.calls == []


def test_antigravity_preference_is_static_and_rejects_an_effort(tmp_path):
    adapter = FakeOmp()
    provider = _antigravity(tmp_path, adapter, status="disconnected", model=None)

    provider.save_notes_preference("gemini-3-pro-high")
    assert provider.saved_notes_model() == "gemini-3-pro-high"
    for bad in ("bad model", "", "x" * 241):
        with pytest.raises(AntigravityError) as invalid:
            provider.save_notes_preference(bad)
        assert invalid.value.code == "invalid_model"
    with pytest.raises(AntigravityError) as effort:
        provider.save_notes_preference("gemini-3-pro-high", "low")
    assert effort.value.code == "invalid_reasoning_effort"
    provider.save_notes_preference("gemini-3-pro-high", "medium")
    assert provider.saved_notes_effort() == "medium"
    assert adapter.catalog_calls == 0 and adapter.calls == []


# --- HTTP contract -------------------------------------------------------------------------


def _settings(tmp_path):
    return Settings(
        data_dir=tmp_path / "data",
        auth_dir=tmp_path / "auth",
        public_url="http://127.0.0.1:8765",
    )


def _connect_chatgpt(provider, *, version=9, model="gpt-6.1-sol", effort="high"):
    client_id = "oaiapp_test-123456"
    with provider.store.locked(write=True) as state:
        state["connection_status"] = "connected"
        state["connection_version"] = version
        state["active_client_id"] = client_id
        state["registrations"][client_id] = {
            "access_token": "local-test-access-token",
            "refresh_token": "local-test-refresh-token",
            "expires_at": time.time() + 3600,
            "scopes": [DIRECT_SCOPE],
            "subject": "test-subject",
            "label": "Test account",
        }
        state["selected_model"] = model
        state["selected_reasoning_effort"] = effort


def _connect_antigravity(provider, *, status="connected", version=4, model=PINNED_MODEL):
    with provider.store.locked(write=True) as state:
        state["connection_status"] = status
        state["connection_version"] = version
        state["selected_model"] = model


class Api:
    def __init__(self, client, settings, app):
        self.client = client
        self.settings = settings
        self.app = app
        self.origin = {"Origin": settings.public_url}

    async def post(self, path, payload):
        return await self.client.post(path, headers=self.origin, json=payload)

    async def put(self, path, payload):
        return await self.client.put(path, headers=self.origin, json=payload)

    async def delete(self, path):
        return await self.client.delete(path, headers=self.origin)

    def run_payload(self, meeting_id, generation_id, request_id, **extra):
        return {
            "request_id": request_id,
            "expected_generation_id": generation_id,
            "expected_source_version": 1,
            "expected_metadata_version": 1,
            **extra,
        }


@asynccontextmanager
async def _api(settings, monkeypatch, *, chatgpt=None, omp=None):
    monkeypatch.setattr(
        "asr_studio.notes_routes.worker_is_healthy",
        lambda _path, *, role=None: True,
    )
    app = create_app(
        settings,
        chatgpt_adapter=chatgpt or FakeChatGPTAdapter(),
        antigravity_adapter=omp or FakeOmp(),
    )
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url=settings.public_url) as client,
    ):
        token = app.state.auth_store.load().token
        session = await client.post(
            "/api/access/session",
            headers={"Origin": settings.public_url},
            json={"token": token},
        )
        assert session.status_code == 204
        yield Api(client, settings, app)


def _task_row(data_dir, task_id):
    with sqlite3.connect(database_path(data_dir)) as connection:
        connection.row_factory = sqlite3.Row
        return dict(connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone())


@pytest.mark.anyio
async def test_notes_preferences_cover_both_providers_without_network_and_survive_restart(
    tmp_path, monkeypatch
):
    settings = _settings(tmp_path)
    chatgpt, omp = FakeChatGPTAdapter(), FakeOmp()
    async with _api(settings, monkeypatch, chatgpt=chatgpt, omp=omp) as api:
        assert (await api.client.get("/api/notes-preferences")).json() == {
            "provider": None,
            "models": {},
            "reasoning_effort": "default",
            "reasoning_efforts": {"chatgpt": "default", "antigravity": "high"},
        }
        # Neither provider is connected: choosing a model is still local and static.
        saved = await api.put(
            "/api/notes-preferences", {"provider": "antigravity", "model": PINNED_MODEL}
        )
        assert saved.status_code == 200
        assert saved.json() == {
            "provider": "antigravity",
            "models": {"antigravity": PINNED_MODEL},
            "reasoning_effort": "default",
            "reasoning_efforts": {"chatgpt": "default", "antigravity": "high"},
        }
        saved = await api.put(
            "/api/notes-preferences",
            {"provider": "chatgpt", "model": "gpt-5.6-luna", "reasoning_effort": "high"},
        )
        assert saved.json() == {
            "provider": "chatgpt",
            "models": {"antigravity": PINNED_MODEL, "chatgpt": "gpt-5.6-luna"},
            "reasoning_effort": "high",
            "reasoning_efforts": {"chatgpt": "high", "antigravity": "high"},
        }
        assert chatgpt.model_calls == [] and omp.catalog_calls == 0

        rejected = [
            ({"provider": "other", "model": "m"}, "invalid_provider"),
            ({"provider": "antigravity", "model": "bad model"}, "invalid_model"),
            (
                {"provider": "antigravity", "model": PINNED_MODEL, "reasoning_effort": "low"},
                "invalid_reasoning_effort",
            ),
            (
                {"provider": "chatgpt", "model": "gpt-6.1-sol", "reasoning_effort": "none"},
                "invalid_reasoning_effort",
            ),
            ({"provider": "chatgpt", "model": ""}, "invalid_preference"),
            ({"provider": "chatgpt", "model": 5}, "invalid_preference"),
            ({"provider": 5, "model": "m"}, "invalid_provider"),
        ]
        for payload, code in rejected:
            response = await api.put("/api/notes-preferences", payload)
            assert response.status_code == 400, payload
            assert response.json()["detail"]["code"] == code, payload
        # Rejected requests change nothing, including the last provider.
        assert (await api.client.get("/api/notes-preferences")).json()["provider"] == "chatgpt"
        assert (await api.put("/api/notes-preferences", {"provider": "chatgpt"})).status_code == 400

    async with _api(settings, monkeypatch) as restarted:
        assert (await restarted.client.get("/api/notes-preferences")).json() == {
            "provider": "chatgpt",
            "models": {"antigravity": PINNED_MODEL, "chatgpt": "gpt-5.6-luna"},
            "reasoning_effort": "high",
            "reasoning_efforts": {"chatgpt": "high", "antigravity": "high"},
        }


@pytest.mark.anyio
async def test_notes_run_pins_only_the_chosen_provider_and_never_picks_a_model(
    tmp_path, monkeypatch
):
    settings = _settings(tmp_path)
    async with _api(settings, monkeypatch) as api:
        meeting_id, generation_id, _ = seed_completed_transcript(settings.data_dir)
        runs = f"/api/meetings/{meeting_id}/notes-runs"
        chatgpt, antigravity = api.app.state.chatgpt_provider, api.app.state.antigravity_provider

        for bad in ("other", "", 5, None, ["antigravity"]):
            response = await api.post(
                runs, api.run_payload(meeting_id, generation_id, "choice-bad", provider=bad)
            )
            assert response.status_code == 400
            assert response.json()["detail"]["code"] == "invalid_provider"

        # AC5: every refusal is explicit, nothing falls back, no task or slot is claimed.
        _connect_chatgpt(chatgpt)  # ChatGPT being ready must not help an Antigravity run.
        refusals = [
            ("disconnected", PINNED_MODEL, "provider_disconnected"),
            ("reconnect_required", PINNED_MODEL, "reconnect_required"),
            ("connected", None, "notes_model_unselected"),
        ]
        for status, model, code in refusals:
            _connect_antigravity(antigravity, status=status, model=model)
            response = await api.post(
                runs,
                api.run_payload(
                    meeting_id, generation_id, "choice-refused", provider="antigravity"
                ),
            )
            assert response.status_code == 409
            assert response.json()["detail"]["code"] == code
            assert "ChatGPT" not in response.json()["detail"]["message"]
        assert notes_state(settings.data_dir, meeting_id)["task"] is None
        assert last_notes_provider(settings.data_dir) is None

        _connect_antigravity(antigravity)
        accepted = await api.post(
            runs,
            api.run_payload(
                meeting_id, generation_id, "choice-antigravity", provider="antigravity"
            ),
        )
        assert accepted.status_code == 202
        task = accepted.json()
        assert (task["provider"], task["model"], task["reasoning_effort"]) == (
            "antigravity",
            PINNED_MODEL,
            "high",
        )
        row = _task_row(settings.data_dir, task["id"])
        assert (row["notes_provider"], row["notes_model"], row["notes_connection_version"]) == (
            "antigravity",
            PINNED_MODEL,
            4,
        )
        assert last_notes_provider(settings.data_dir) == "antigravity"
        assert (await api.client.get("/api/notes-preferences")).json()["provider"] == "antigravity"

        retry = await api.post(
            runs,
            api.run_payload(
                meeting_id, generation_id, "choice-antigravity", provider="antigravity"
            ),
        )
        assert retry.json()["id"] == task["id"]
        conflict = await api.post(
            runs, api.run_payload(meeting_id, generation_id, "choice-antigravity")
        )
        assert conflict.status_code == 409
        assert conflict.json()["detail"]["code"] == "idempotency_conflict"

        # AC3: changing the preference now only affects the next run.
        await api.put("/api/notes-preferences", {"provider": "chatgpt", "model": "gpt-6-luna"})
        await api.put("/api/notes-preferences", {"provider": "antigravity", "model": "gemini-x"})
        unchanged = _task_row(settings.data_dir, task["id"])
        for column in ("notes_provider", "notes_model", "notes_connection_version"):
            assert unchanged[column] == row[column]
        assert cancel_task(settings.data_dir, task["id"])["status"] == "canceled"

        # The default provider is ChatGPT and its pin is read from its own store only.
        _connect_antigravity(antigravity, status="disconnected")
        default = await api.post(runs, api.run_payload(meeting_id, generation_id, "choice-default"))
        assert default.status_code == 202
        assert default.json()["provider"] == "chatgpt"
        assert default.json()["model"] == "gpt-6-luna"
        assert last_notes_provider(settings.data_dir) == "chatgpt"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("status", "model", "code"),
    [
        ("disconnected", "gpt-6.1-sol", "provider_disconnected"),
        ("reconnect_required", "gpt-6.1-sol", "reconnect_required"),
        ("connected", None, "notes_model_unselected"),
    ],
)
async def test_chatgpt_refusals_are_explicit_even_when_antigravity_is_ready(
    tmp_path, monkeypatch, status, model, code
):
    settings = _settings(tmp_path)
    async with _api(settings, monkeypatch) as api:
        meeting_id, generation_id, _ = seed_completed_transcript(settings.data_dir)
        _connect_antigravity(api.app.state.antigravity_provider)
        _connect_chatgpt(api.app.state.chatgpt_provider, model=model)
        with api.app.state.chatgpt_provider.store.locked(write=True) as state:
            state["connection_status"] = status

        response = await api.post(
            f"/api/meetings/{meeting_id}/notes-runs",
            api.run_payload(meeting_id, generation_id, "chatgpt-refused", provider="chatgpt"),
        )

        assert response.status_code == 409
        assert response.json()["detail"]["code"] == code
        assert notes_state(settings.data_dir, meeting_id)["task"] is None


@pytest.mark.anyio
@pytest.mark.parametrize("pinned", ["chatgpt", "antigravity"])
async def test_active_notes_task_blocks_only_its_own_provider_connection(
    tmp_path, monkeypatch, pinned
):
    settings = _settings(tmp_path)
    async with _api(settings, monkeypatch) as api:
        meeting_id, generation_id, _ = seed_completed_transcript(settings.data_dir)
        chatgpt, antigravity = api.app.state.chatgpt_provider, api.app.state.antigravity_provider
        _connect_chatgpt(chatgpt)
        _connect_antigravity(antigravity)
        accepted = await api.post(
            f"/api/meetings/{meeting_id}/notes-runs",
            api.run_payload(meeting_id, generation_id, "guard-run", provider=pinned),
        )
        assert accepted.status_code == 202

        listed = (await api.client.get("/api/providers")).json()["providers"]
        by_id = {provider["id"]: provider for provider in listed}
        assert by_id[pinned]["in_use"] is True
        assert by_id[pinned]["in_use_reason"] == "notes"
        other = "antigravity" if pinned == "chatgpt" else "chatgpt"
        assert by_id[other]["in_use"] is False
        assert by_id[other]["in_use_reason"] is None

        blocked_calls = {
            "chatgpt": [
                ("GET", "/api/providers/chatgpt/models", None),
                ("POST", "/api/providers/chatgpt/login-sessions", {}),
                ("POST", "/api/providers/chatgpt/verify", {"model": "gpt-6.1-sol"}),
                ("DELETE", "/api/providers/chatgpt/connection", None),
            ],
            "antigravity": [
                ("GET", "/api/providers/antigravity/models", None),
                ("POST", "/api/providers/antigravity/login-sessions", {}),
                ("POST", "/api/providers/antigravity/verify", {"model": PINNED_MODEL}),
                ("PUT", "/api/providers/antigravity/preference", {"model": PINNED_MODEL}),
                ("DELETE", "/api/providers/antigravity/connection", None),
            ],
        }
        for method, path, body in blocked_calls[pinned]:
            response = await api.client.request(
                method, path, headers=api.origin, **({"json": body} if body is not None else {})
            )
            assert response.status_code == 409, path
            assert response.json()["detail"]["code"] == "provider_in_use", path

        other_models = await api.client.get(f"/api/providers/{other}/models")
        assert other_models.status_code == 200
        # Choosing models stays possible for both providers during a run.
        for provider, model in (("chatgpt", "gpt-6-luna"), ("antigravity", "gemini-x")):
            saved = await api.put("/api/notes-preferences", {"provider": provider, "model": model})
            assert saved.status_code == 200
        disconnected = await api.delete(f"/api/providers/{other}/connection")
        assert disconnected.status_code == 200
        assert disconnected.json()["status"] == "disconnected"
        with (chatgpt if pinned == "chatgpt" else antigravity).store.locked() as state:
            assert state["connection_status"] == "connected"


# --- runner and worker ---------------------------------------------------------------------


def _accept(data_dir, provider, provider_id, request_id, meeting_id, generation_id, **extra):
    return accept_notes_task(
        data_dir,
        provider=provider,
        provider_id=provider_id,
        meeting_id=meeting_id,
        request_id=f"request-{request_id}",
        expected_generation_id=generation_id,
        expected_source_version=1,
        expected_metadata_version=1,
        worker_ready=True,
        **extra,
    )


def _lengthen_transcript(data_dir):
    with sqlite3.connect(database_path(data_dir)) as connection:
        connection.execute("UPDATE transcript_segments SET text = ?", ("x" * 16_000,))


def _transcript_rows(data_dir):
    with sqlite3.connect(database_path(data_dir)) as connection:
        return connection.execute(
            "SELECT id, sequence, text, speaker_id FROM transcript_segments ORDER BY id"
        ).fetchall()


class ProviderPair:
    """Both providers over one provider directory, as the app and its notes runner see them."""

    def __init__(self, tmp_path, monkeypatch, *, chatgpt_responder=None, omp=None):
        self.directory = tmp_path / "provider"
        self.chatgpt_adapter = NotesOnlyAdapter(responder=chatgpt_responder or _chatgpt_answer)
        self.omp = omp or FakeOmp()
        self.chatgpt = _provider(tmp_path, self.chatgpt_adapter)
        self.antigravity = _antigravity(tmp_path, self.omp)
        self.chatgpt.save_notes_preference(model="gpt-6.1-sol", reasoning_effort="high")
        monkeypatch.setenv("ASR_PROVIDER_DIR", str(self.directory))
        monkeypatch.setattr(
            notes_runner,
            "ChatGPTProvider",
            lambda directory, callback_url: ChatGPTProvider(
                directory, callback_url=callback_url, adapter=self.chatgpt_adapter
            ),
        )
        monkeypatch.setattr(
            notes_runner,
            "AntigravityProvider",
            lambda directory: AntigravityProvider(directory, adapter=self.omp),
        )

    def provider(self, provider_id):
        return self.chatgpt if provider_id == "chatgpt" else self.antigravity

    def calls(self, provider_id):
        if provider_id == "chatgpt":
            return [(call.model, call.prompt) for call in self.chatgpt_adapter.calls]
        return self.omp.calls


def _chatgpt_answer(prompt, _limit):
    return _document_for_prompt(prompt)


def _run_notes(data_dir, capsys, task_id, owner_id):
    """Run the notes child in process and return its last event and the saved revision."""
    capsys.readouterr()
    code = notes_runner.run_notes_task(data_dir, task_id, owner_id)
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    last = events[-1]
    assert (code == 0) == (last["type"] == "result")
    revision = None
    if last["type"] == "result":
        revision = publish_notes_revision(
            data_dir,
            task_id,
            owner_id=owner_id,
            markdown=last["markdown"],
            elapsed_seconds=0.5,
            coverage=last["coverage"],
        )
        assert revision is not None
    return last, revision


@pytest.mark.parametrize("provider_id", ["chatgpt", "antigravity"])
def test_preference_change_during_an_active_run_never_changes_the_pinned_calls(
    tmp_path, monkeypatch, capsys, provider_id
):
    data_dir = tmp_path / "data"
    meeting_id, generation_id, _ = seed_completed_transcript(data_dir)
    _lengthen_transcript(data_dir)
    pair = ProviderPair(tmp_path, monkeypatch)
    pinned_model = "gpt-6.1-sol" if provider_id == "chatgpt" else PINNED_MODEL
    task = _accept(
        data_dir, pair.provider(provider_id), provider_id, "pin-1", meeting_id, generation_id
    )
    claim_next_task(data_dir, owner_id="owner-pin")

    def switch(prompt, _limit=None):
        pair.chatgpt.save_notes_preference(model="gpt-6-luna", reasoning_effort="none")
        pair.antigravity.save_notes_preference("gemini-other")
        return _answer(prompt)

    pair.chatgpt_adapter.responder = switch
    pair.omp.responder = switch

    event, revision = _run_notes(data_dir, capsys, task["id"], "owner-pin")

    assert event["type"] == "result"
    calls = pair.calls(provider_id)
    assert len(calls) == 3
    assert {model for model, _ in calls} == {pinned_model}
    if provider_id == "chatgpt":
        assert {call.reasoning_effort for call in pair.chatgpt_adapter.calls} == {"high"}
    assert (revision["provider"], revision["model"]) == (provider_id, pinned_model)
    assert pair.calls("antigravity" if provider_id == "chatgpt" else "chatgpt") == []
    # The next run sees the changed preference.
    assert pair.chatgpt.saved_notes_settings() == ("gpt-6-luna", "none")
    assert pair.antigravity.saved_notes_model() == "gemini-other"


@pytest.mark.parametrize("provider_id", ["chatgpt", "antigravity"])
@pytest.mark.parametrize(
    "scenario",
    ["disconnected", "reconnect_required", "connection_changed", "quota", "model_unavailable"],
)
def test_runner_reports_provider_state_and_errors_without_switching(
    tmp_path, monkeypatch, capsys, provider_id, scenario
):
    data_dir = tmp_path / "data"
    meeting_id, generation_id, _ = seed_completed_transcript(data_dir)
    pair = ProviderPair(tmp_path, monkeypatch)
    provider = pair.provider(provider_id)
    task = _accept(data_dir, provider, provider_id, "state-1", meeting_id, generation_id)
    claim_next_task(data_dir, owner_id="owner-state")
    other = "antigravity" if provider_id == "chatgpt" else "chatgpt"

    expected_code = {
        "disconnected": "provider_disconnected",
        "reconnect_required": "reconnect_required",
        "connection_changed": "connection_changed",
        "quota": "provider_quota",
        "model_unavailable": "model_unavailable",
    }[scenario]
    if scenario in {"disconnected", "reconnect_required"}:
        with provider.store.locked(write=True) as state:
            state["connection_status"] = scenario
            state["connection_version"] += 1
    elif scenario == "connection_changed":
        with provider.store.locked(write=True) as state:
            state["connection_version"] += 1
    elif provider_id == "antigravity":
        pair.omp.replies = [AntigravityError(*_OMP_ERRORS[scenario])]
    else:
        pair.chatgpt_adapter.responder = None
        pair.chatgpt_adapter.replies = [ChatGPTError(*_CHATGPT_ERRORS[scenario])]

    event, revision = _run_notes(data_dir, capsys, task["id"], "owner-state")

    assert revision is None
    assert event["type"] == "error"
    assert event["code"] == expected_code
    other_label = "ChatGPT" if provider_id == "antigravity" else "Antigravity"
    assert other_label not in event["message"]
    assert len(event["message"]) <= 500
    # No provider call was repeated on the other provider, and the same provider got at most one.
    assert pair.calls(other) == []
    assert len(pair.calls(provider_id)) <= 1
    assert last_notes_provider(data_dir) == provider_id


_OMP_ERRORS = {
    "quota": (429, "provider_quota", "quota"),
    "model_unavailable": (422, "model_unavailable", "model"),
}
_CHATGPT_ERRORS = {
    "quota": (429, "provider_quota", "quota"),
    "model_unavailable": (422, "model_unavailable", "model"),
}


def test_regeneration_appends_revisions_and_keeps_earlier_edits_and_transcript(
    tmp_path, monkeypatch, capsys
):
    data_dir = tmp_path / "data"
    meeting_id, generation_id, _ = seed_completed_transcript(data_dir)
    pair = ProviderPair(tmp_path, monkeypatch)
    transcript = _transcript_rows(data_dir)

    first = _accept(data_dir, pair.chatgpt, "chatgpt", "regen-1", meeting_id, generation_id)
    claim_next_task(data_dir, owner_id="owner-regen-1")
    _, chatgpt_revision = _run_notes(data_dir, capsys, first["id"], "owner-regen-1")
    edited = update_note(
        data_dir,
        chatgpt_revision["id"],
        expected_edit_version=1,
        markdown="# Operator edit\n\nKept.\n",
    )

    second = _accept(
        data_dir, pair.antigravity, "antigravity", "regen-2", meeting_id, generation_id
    )
    claim_next_task(data_dir, owner_id="owner-regen-2")
    _, antigravity_revision = _run_notes(data_dir, capsys, second["id"], "owner-regen-2")

    state = notes_state(data_dir, meeting_id)
    assert [item["id"] for item in state["revisions"]] == [
        antigravity_revision["id"],
        chatgpt_revision["id"],
    ]
    assert [item["provider"] for item in state["revisions"]] == ["antigravity", "chatgpt"]
    assert state["task"]["id"] == second["id"] and state["task"]["status"] == "succeeded"
    kept = get_note(data_dir, chatgpt_revision["id"])
    assert kept["markdown"] == "# Operator edit\n\nKept.\n"
    assert kept["edit_version"] == edited["edit_version"] == 2
    assert antigravity_revision["markdown"].startswith("##")
    assert antigravity_revision["source_snapshot_id"] != chatgpt_revision["source_snapshot_id"]
    assert _transcript_rows(data_dir) == transcript


@pytest.mark.parametrize(
    ("failing", "second"), [("antigravity", "chatgpt"), ("chatgpt", "antigravity")]
)
def test_after_a_failure_the_operator_can_start_a_new_run_with_the_other_provider(
    tmp_path, monkeypatch, capsys, failing, second
):
    data_dir = tmp_path / "data"
    meeting_id, generation_id, _ = seed_completed_transcript(data_dir)
    pair = ProviderPair(tmp_path, monkeypatch)
    if failing == "antigravity":
        pair.omp.replies = [AntigravityError(429, "provider_quota", "quota")]
    else:
        pair.chatgpt_adapter.responder = None
        pair.chatgpt_adapter.replies = [ChatGPTError(429, "provider_quota", "quota")]

    failed = _accept(
        data_dir, pair.provider(failing), failing, "retry-1", meeting_id, generation_id
    )
    claim_next_task(data_dir, owner_id="owner-retry-1")
    event, revision = _run_notes(data_dir, capsys, failed["id"], "owner-retry-1")
    assert (event["type"], event["code"], revision) == ("error", "provider_quota", None)
    # The worker records a terminal failure; emulate it the way the worker does.
    fail_task(
        data_dir,
        failed["id"],
        owner_id="owner-retry-1",
        error_code=event["code"],
        message=event["message"],
        elapsed_seconds=0.1,
    )
    assert len(pair.calls(failing)) <= 1

    retry = _accept(data_dir, pair.provider(second), second, "retry-2", meeting_id, generation_id)
    claim_next_task(data_dir, owner_id="owner-retry-2")
    event, revision = _run_notes(data_dir, capsys, retry["id"], "owner-retry-2")

    assert event["type"] == "result"
    assert revision["provider"] == second
    assert _task_row(data_dir, failed["id"])["status"] == "failed"
    assert _task_row(data_dir, failed["id"])["error_code"] == "provider_quota"
    assert _task_row(data_dir, retry["id"])["status"] == "succeeded"
    assert len(pair.calls(failing)) <= 1


@pytest.mark.anyio
@pytest.mark.parametrize("connected", ["chatgpt", "antigravity"])
async def test_each_provider_runs_when_only_it_is_connected_across_an_app_restart(
    tmp_path, monkeypatch, capsys, connected
):
    settings = _settings(tmp_path)
    model = "gpt-6.1-sol" if connected == "chatgpt" else PINNED_MODEL
    async with _api(settings, monkeypatch) as first:
        meeting_id, generation_id, _ = seed_completed_transcript(settings.data_dir)
        if connected == "chatgpt":
            _connect_chatgpt(first.app.state.chatgpt_provider)
        else:
            _connect_antigravity(first.app.state.antigravity_provider)
        saved = await first.put("/api/notes-preferences", {"provider": connected, "model": model})
        assert saved.status_code == 200

    omp = FakeOmp()
    chatgpt_adapter = NotesOnlyAdapter(responder=_chatgpt_answer)
    async with _api(settings, monkeypatch, omp=omp) as restarted:
        assert (await restarted.client.get("/api/notes-preferences")).json()["provider"] == (
            connected
        )
        views = (await restarted.client.get("/api/providers")).json()["providers"]
        assert {view["id"]: view["status"] for view in views} == {
            "chatgpt": "connected" if connected == "chatgpt" else "disconnected",
            "antigravity": "connected" if connected == "antigravity" else "disconnected",
        }
        response = await restarted.post(
            f"/api/meetings/{meeting_id}/notes-runs",
            restarted.run_payload(meeting_id, generation_id, "restart-1", provider=connected),
        )
        assert response.status_code == 202
        task_id = response.json()["id"]

    monkeypatch.setenv("ASR_PROVIDER_DIR", str(settings.provider_dir))
    monkeypatch.setattr(
        notes_runner,
        "ChatGPTProvider",
        lambda directory, callback_url: ChatGPTProvider(
            directory, callback_url=callback_url, adapter=chatgpt_adapter
        ),
    )
    monkeypatch.setattr(
        notes_runner,
        "AntigravityProvider",
        lambda directory: AntigravityProvider(directory, adapter=omp),
    )
    claim_next_task(settings.data_dir, owner_id="owner-restart")
    event, revision = _run_notes(settings.data_dir, capsys, task_id, "owner-restart")

    assert event["type"] == "result"
    assert (revision["provider"], revision["model"]) == (connected, model)
    other_calls = chatgpt_adapter.calls if connected == "antigravity" else omp.calls
    assert other_calls == []


# --- real runner process with a fake OMP executable -------------------------------------------

FAKE_OMP = """#!/usr/bin/env python3
import json
import os
import sys
import time
from pathlib import Path

agent = Path(os.environ["PI_CODING_AGENT_DIR"])
mode = (agent / "mode").read_text().strip() if (agent / "mode").exists() else "ok"
model = None


def reply(prompt):
    schema = json.loads(prompt.split("allows:\\n", 1)[1])
    ids = schema.get("$defs", {}).get("source_id", {}).get("enum", [])
    overview = [{"text": "A concise summary.", "source_ids": ids[:1]}] if ids else []
    return json.dumps(
        {
            "schema_version": 1,
            "overview": overview,
            "key_topics": [],
            "decisions": [],
            "open_questions": [],
            "action_items": [],
            "confirmation_items": [],
        }
    )


level = "high"
print(json.dumps({"type": "ready"}), flush=True)
for line in sys.stdin:
    command = json.loads(line)
    request_id = command.get("id")
    kind = command.get("type")
    data = None
    if kind == "set_thinking_level":
        level = command["level"]
        data = {}
    elif kind == "get_state":
        data = {"dumpTools": [], "thinkingLevel": level}
    elif kind in {"set_auto_compaction", "set_auto_retry"}:
        data = {}
    elif kind == "set_model":
        model = command["modelId"]
        data = {}
    elif kind == "prompt":
        with (agent / "calls.jsonl").open("a") as handle:
            record = {"pid": os.getpid(), "pgid": os.getpgid(0), "model": model, "level": level}
            handle.write(json.dumps(record))
            handle.write("\\n")
        if mode == "hang":
            (agent / "hang-pid").write_text(str(os.getpid()))
            time.sleep(120)
        accepted = {
            "type": "response", "id": request_id, "command": "prompt",
            "success": True, "data": {"agentInvoked": True},
        }
        print(json.dumps(accepted), flush=True)
        if mode == "finish_reason":
            # S-2 frame shape; S-3 (docs/agents/development.md) message for an error finish reason.
            failed = {
                "type": "prompt_result", "id": request_id, "status": "error",
                "error": {"provider": "google-antigravity", "model": model, "retryable": False,
                          "message": "Generation failed with finish reason: "
                                     "MALFORMED_FUNCTION_CALL"},
            }
            print(json.dumps(failed), flush=True)
            break
        if mode == "quota":
            # S-2 (docs/agents/development.md): a provider-run error is status "error" with an
            # error object carrying message, retryable, provider, model and optional httpStatus.
            failed = {
                "type": "prompt_result", "id": request_id, "status": "error",
                "error": {"httpStatus": 429, "provider": "google-antigravity", "model": model,
                          "message": "quota exhausted", "retryable": False},
            }
            print(json.dumps(failed), flush=True)
            break
        if mode == "network":
            # S-2 again: the connection dropped after the prompt was sent, and the error object
            # still says retryable. Neither the adapter nor OMP may send the prompt a second time.
            failed = {
                "type": "prompt_result", "id": request_id, "status": "error",
                "error": {"provider": "google-antigravity", "model": model, "retryable": True,
                          "message": "Network connection lost"},
            }
            print(json.dumps(failed), flush=True)
            break
        earlier_calls = len((agent / "calls.jsonl").read_text().splitlines()) - 1
        broken = mode == "invalid" or (mode == "fail_after_first" and earlier_calls > 0)
        text = "not json" if broken else reply(command["message"])
        message = {"type": "message_end",
                   "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}
        print(json.dumps(message), flush=True)
        done = {"type": "prompt_result", "id": request_id, "status": "completed"}
        print(json.dumps(done), flush=True)
        continue
    print(json.dumps({"type": "response", "id": request_id, "command": kind,
                      "success": True, "data": data}), flush=True)
"""


@pytest.fixture
def fake_omp_binary(tmp_path, monkeypatch):
    directory = tmp_path / "bin"
    directory.mkdir()
    executable = directory / "omp"
    executable.write_text(FAKE_OMP, encoding="utf-8")
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", f"{directory}{os.pathsep}{os.environ['PATH']}")
    agent = tmp_path / "provider" / "omp" / "agent"
    agent.mkdir(parents=True)
    return agent


def _omp_calls(agent):
    path = agent / "calls.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def _run_with_worker(settings, task, owner_id):
    lock_fd = acquire_worker_lock(settings.data_dir)
    assert lock_fd is not None
    try:
        worker._run_notes_task(settings, lock_fd, task, threading.Event())
    finally:
        os.close(lock_fd)


def _antigravity_task(tmp_path, request_id="real-1", *, chunked=False):
    settings = Settings(data_dir=tmp_path / "data", auth_dir=tmp_path / "auth")
    meeting_id, generation_id, _ = seed_completed_transcript(settings.data_dir)
    if chunked:
        _lengthen_transcript(settings.data_dir)
    provider = _antigravity(tmp_path, FakeOmp())
    accepted = _accept(
        settings.data_dir, provider, "antigravity", request_id, meeting_id, generation_id
    )
    task = claim_next_task(settings.data_dir, owner_id=f"owner-{request_id}")
    assert task is not None and task["id"] == accepted["id"]
    return settings, meeting_id, task


@pytest.mark.parametrize("chunked", [False, True])
def test_antigravity_run_through_worker_publishes_one_revision_with_a_fresh_omp_per_call(
    tmp_path, fake_omp_binary, chunked
):
    settings, meeting_id, task = _antigravity_task(tmp_path, chunked=chunked)

    _run_with_worker(settings, task, "owner-real-1")

    state = notes_state(settings.data_dir, meeting_id)
    assert state["task"]["status"] == "succeeded"
    assert [item["provider"] for item in state["revisions"]] == ["antigravity"]
    revision = state["revisions"][0]
    assert (revision["model"], revision["reasoning_effort"]) == (PINNED_MODEL, "high")
    assert revision["markdown"].startswith("##")
    calls = _omp_calls(fake_omp_binary)
    assert len(calls) == (3 if chunked else 1)
    assert {call["model"] for call in calls} == {PINNED_MODEL}
    assert len({call["pid"] for call in calls}) == len(calls)
    # Inference OMP stays in the runner's process group instead of a session of its own.
    assert all(call["pgid"] != call["pid"] for call in calls)


@pytest.mark.parametrize(
    ("mode", "code"),
    [
        ("invalid", "notes_invalid_output"),
        ("quota", "provider_quota"),
        ("finish_reason", "provider_invalid_response"),
    ],
)
def test_antigravity_failures_through_worker_fail_the_task_without_a_revision(
    tmp_path, fake_omp_binary, mode, code
):
    settings, meeting_id, task = _antigravity_task(tmp_path, "real-fail")
    (fake_omp_binary / "mode").write_text(mode)

    _run_with_worker(settings, task, "owner-real-fail")

    state = notes_state(settings.data_dir, meeting_id)
    assert state["revisions"] == []
    assert state["task"]["status"] == "failed"
    assert state["task"]["error_code"] == code
    assert "ChatGPT" not in state["task"]["error_message"]
    assert len(_omp_calls(fake_omp_binary)) == (2 if mode == "invalid" else 1)
    assert state["task"]["provider"] == "antigravity"
    assert state["source_locked"] is False


def test_omp_child_stops_with_the_notes_runner_process_group(tmp_path, fake_omp_binary):
    settings, _, task = _antigravity_task(tmp_path, "real-kill")
    (fake_omp_binary / "mode").write_text("hang")
    process = subprocess.Popen(
        worker._notes_command(task, "owner-real-kill", settings.data_dir),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        start_new_session=True,
        env={
            **os.environ,
            "ASR_DATA_DIR": str(settings.data_dir),
            "ASR_PROVIDER_DIR": str(settings.provider_dir),
        },
    )
    omp_pid = None
    try:
        marker = fake_omp_binary / "hang-pid"
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not marker.exists():
            assert process.poll() is None, "The notes runner exited before OMP started."
            time.sleep(0.05)
        assert marker.exists(), "OMP did not start an inference call."
        omp_pid = int(marker.read_text())
        assert os.getpgid(omp_pid) == process.pid

        assert worker.terminate_and_reap_process_group(
            process, term_grace_seconds=0.5, kill_grace_seconds=2.0
        )

        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                os.kill(omp_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.02)
        with pytest.raises(ProcessLookupError):
            os.kill(omp_pid, 0)
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        if process.stdout is not None:
            process.stdout.close()
        if omp_pid is not None:
            with suppress(ProcessLookupError):
                os.kill(omp_pid, 9)


def test_task_summary_exposes_the_pinned_provider_only_for_notes(tmp_path):
    settings, meeting_id, _ = _antigravity_task(tmp_path, "summary-1")
    summary = notes_state(settings.data_dir, meeting_id)["task"]
    assert (summary["provider"], summary["model"], summary["reasoning_effort"]) == (
        "antigravity",
        PINNED_MODEL,
        "high",
    )
    with pytest.raises(TaskError) as invalid:
        accept_notes_task(
            settings.data_dir,
            provider=_antigravity(tmp_path, FakeOmp()),
            provider_id="bogus",
            meeting_id=meeting_id,
            request_id="summary-2",
            expected_generation_id="g",
            expected_source_version=1,
            expected_metadata_version=1,
            worker_ready=True,
        )
    assert invalid.value.code == "invalid_provider"


# --- Review round 1 backend fixes: R1 login serialization, R3 confirmed choice ---


def _storage_counts(data_dir):
    with sqlite3.connect(database_path(data_dir)) as connection:
        tasks = connection.execute("SELECT count(*) FROM tasks WHERE kind = 'notes'").fetchone()[0]
        sources = connection.execute("SELECT count(*) FROM notes_sources").fetchone()[0]
        slot = connection.execute(
            "SELECT active_task_id FROM processing_slot WHERE slot_id = 1"
        ).fetchone()[0]
    return tasks, sources, slot


class LoginOmp(FakeOmp):
    """Login double: records calls and stays pending until released or cancelled."""

    def __init__(self):
        super().__init__()
        self.login_calls = 0
        self.release = threading.Event()

    def login(self, interaction):
        self.login_calls += 1
        interaction.publish({"authorization_url": "https://accounts.google.com/o/oauth2/auth?s=1"})
        while not self.release.wait(0.01):
            if interaction.cancel_event.is_set():
                raise AntigravityError(409, "login_cancelled", "cancelled")


def _wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition not reached"
        time.sleep(0.01)


def test_notes_acceptance_is_refused_while_an_antigravity_login_is_pending(tmp_path):
    settings = _settings(tmp_path)
    meeting_id, generation_id, _ = seed_completed_transcript(settings.data_dir)
    adapter = LoginOmp()
    provider = _antigravity(tmp_path, adapter)
    session = provider.start_login()

    with pytest.raises(AntigravityError) as refused:
        _accept(settings.data_dir, provider, "antigravity", "login-1", meeting_id, generation_id)

    assert (refused.value.status_code, refused.value.code) == (409, "login_in_progress")
    assert "sign-in" in refused.value.public_message
    assert _storage_counts(settings.data_dir) == (0, 0, None)
    # Cancelling the sign-in lifts the refusal.
    provider.cancel_login(session["id"])
    accepted = _accept(
        settings.data_dir, provider, "antigravity", "login-2", meeting_id, generation_id
    )
    assert accepted["provider"] == "antigravity"


def test_login_cannot_replace_credentials_while_the_operation_lock_is_held(tmp_path):
    adapter = LoginOmp()
    provider = _antigravity(tmp_path, adapter)
    with provider.store.operation():  # a runner (or another process) is using the account
        session = provider.start_login()
        time.sleep(0.3)
        assert adapter.login_calls == 0
        assert provider.provider_view()["connection_version"] == 4
    adapter.release.set()
    _wait_for(lambda: provider.login_session(session["id"])["status"] == "connected")
    assert adapter.login_calls == 1
    assert provider.provider_view()["connection_version"] == 5


def test_login_rechecks_the_active_task_guard_after_taking_the_operation_lock(tmp_path):
    adapter = LoginOmp()
    active = threading.Event()

    def guard():
        if active.is_set():
            raise AntigravityError(409, "provider_in_use", "Wait for notes generation.")

    provider = AntigravityProvider(tmp_path / "provider", adapter=adapter, active_task_guard=guard)
    provider.ensure()
    _connect_antigravity(provider)
    with provider.store.operation():
        session = provider.start_login()  # the guard passed: no task was active yet
        active.set()  # a notes task is accepted while the login waits for the lock
    _wait_for(lambda: provider.login_session(session["id"])["status"] == "failed")

    assert provider.login_session(session["id"])["error"]["code"] == "provider_in_use"
    assert adapter.login_calls == 0
    assert provider.provider_view()["connection_version"] == 4


def test_restarting_a_pending_login_yields_a_usable_session(tmp_path):
    adapter = LoginOmp()
    provider = _antigravity(tmp_path, adapter)
    first = provider.start_login()
    second = provider.start_login()

    assert second["id"] != first["id"]
    assert second["status"] == "pending" and second["authorization_url"]
    assert provider.login_session(first["id"])["status"] == "cancelled"
    adapter.release.set()
    _wait_for(lambda: provider.login_session(second["id"])["status"] == "connected")
    assert provider.login_session(first["id"])["status"] == "cancelled"
    assert provider.provider_view()["connection_version"] == 5
    assert adapter.login_calls == 2


def test_cancelling_a_pending_login_releases_the_operation_lock(tmp_path):
    adapter = LoginOmp()
    provider = _antigravity(tmp_path, adapter)
    session = provider.start_login()
    provider.cancel_login(session["id"])

    _wait_for(lambda: _lock_is_free(provider))
    assert provider.provider_view()["connection_version"] == 4


def _lock_is_free(provider):
    try:
        with provider.store.operation(timeout=0):
            return True
    except AntigravityError:
        return False


@pytest.mark.anyio
@pytest.mark.parametrize("provider_id", ["chatgpt", "antigravity"])
async def test_confirmed_choice_must_match_the_saved_preference(tmp_path, monkeypatch, provider_id):
    settings = _settings(tmp_path)
    async with _api(settings, monkeypatch) as api:
        meeting_id, generation_id, _ = seed_completed_transcript(settings.data_dir)
        runs = f"/api/meetings/{meeting_id}/notes-runs"
        _connect_chatgpt(api.app.state.chatgpt_provider)
        _connect_antigravity(api.app.state.antigravity_provider)
        model = "gpt-6.1-sol" if provider_id == "chatgpt" else PINNED_MODEL
        effort = "high"

        def body(request_id, **extra):
            return api.run_payload(
                meeting_id, generation_id, request_id, provider=provider_id, **extra
            )

        mismatches = [{"expected_model": "someone-else"}]
        if provider_id == "chatgpt":
            mismatches += [{"expected_model": model, "expected_reasoning_effort": "low"}]
        else:
            mismatches += [{"expected_model": model, "expected_reasoning_effort": "medium"}]
        for index, extra in enumerate(mismatches):
            response = await api.post(runs, body(f"mismatch-{index}", **extra))
            assert response.status_code == 409, extra
            assert response.json()["detail"]["code"] == "notes_preference_changed"
            assert "reload" in response.json()["detail"]["message"].lower()
            assert _storage_counts(settings.data_dir) == (0, 0, None)

        extra = {"expected_model": model, "expected_reasoning_effort": effort}
        accepted = await api.post(runs, body("matching-1", **extra))
        assert accepted.status_code == 202
        assert (accepted.json()["model"], accepted.json()["reasoning_effort"]) == (model, effort)
        # Replay is idempotent with the same fields and conflicts with different ones.
        replay = await api.post(runs, body("matching-1", **extra))
        assert replay.status_code == 202 and replay.json()["id"] == accepted.json()["id"]
        different = await api.post(runs, body("matching-1"))
        assert different.status_code == 409
        assert different.json()["detail"]["code"] == "idempotency_conflict"
        assert _storage_counts(settings.data_dir)[0] == 1


@pytest.mark.anyio
async def test_confirmed_choice_is_optional_and_validated(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    async with _api(settings, monkeypatch) as api:
        meeting_id, generation_id, _ = seed_completed_transcript(settings.data_dir)
        runs = f"/api/meetings/{meeting_id}/notes-runs"
        _connect_chatgpt(api.app.state.chatgpt_provider)
        _connect_antigravity(api.app.state.antigravity_provider)

        invalid = [
            ("chatgpt", {"expected_model": 5}, "invalid_expected_model"),
            ("chatgpt", {"expected_model": None}, "invalid_expected_model"),
            ("chatgpt", {"expected_model": ""}, "invalid_expected_model"),
            ("chatgpt", {"expected_reasoning_effort": 5}, "invalid_reasoning_effort"),
            ("antigravity", {"expected_reasoning_effort": "low"}, "invalid_reasoning_effort"),
        ]
        for provider_id, extra, code in invalid:
            response = await api.post(
                runs,
                api.run_payload(
                    meeting_id, generation_id, "invalid-1", provider=provider_id, **extra
                ),
            )
            assert response.status_code == 400, extra
            assert response.json()["detail"]["code"] == code
        assert _storage_counts(settings.data_dir) == (0, 0, None)

        # Absent fields keep today's behavior.
        legacy = await api.post(runs, api.run_payload(meeting_id, generation_id, "legacy-1"))
        assert legacy.status_code == 202


def test_request_hash_changes_only_when_confirmed_fields_are_present():
    import hashlib

    from asr_studio.notes import _request_hash

    base = {
        "meeting_id": "m",
        "expected_generation_id": "g",
        "expected_source_version": 1,
        "expected_metadata_version": 1,
    }
    legacy = {
        "expected_generation_id": "g",
        "expected_metadata_version": 1,
        "expected_source_version": 1,
        "meeting_id": "m",
    }
    encoded = json.dumps(legacy, sort_keys=True, separators=(",", ":"))
    assert _request_hash(**base) == hashlib.sha256(encoded.encode()).hexdigest()
    with_model = _request_hash(**base, expected_model="x")
    assert with_model != _request_hash(**base)
    assert _request_hash(**base, expected_model="x", expected_reasoning_effort="high") != with_model


def test_finish_reason_errors_are_invalid_responses_but_a_dropped_stream_is_unavailable():
    def code(message):
        frame = {"type": "prompt_result", "status": "error", "error": {"message": message}}
        with pytest.raises(AntigravityError) as raised:
            OmpRpcAdapter.raise_rpc_error(frame)
        return raised.value.code

    assert code("Generation failed with finish reason: SAFETY") == "provider_invalid_response"
    # S-3: a stream without any finish reason is a dropped connection.
    assert (
        code(
            "Cloud Code Assist stream ended without a finish reason "
            "(connection dropped or response truncated)"
        )
        == "provider_unavailable"
    )


def test_antigravity_run_pins_and_applies_the_saved_medium_level(tmp_path, fake_omp_binary):
    settings = Settings(data_dir=tmp_path / "data", auth_dir=tmp_path / "auth")
    meeting_id, generation_id, _ = seed_completed_transcript(settings.data_dir)
    provider = _antigravity(tmp_path, FakeOmp())
    provider.save_notes_preference(PINNED_MODEL, "medium")
    _accept(settings.data_dir, provider, "antigravity", "medium-1", meeting_id, generation_id)
    task = claim_next_task(settings.data_dir, owner_id="owner-medium-1")
    assert task is not None and task["notes_reasoning_effort"] == "medium"

    provider.save_notes_preference(PINNED_MODEL, "high")  # applies to the next run only
    _run_with_worker(settings, task, "owner-medium-1")

    state = notes_state(settings.data_dir, meeting_id)
    assert state["revisions"][0]["reasoning_effort"] == "medium"
    assert {call["level"] for call in _omp_calls(fake_omp_binary)} == {"medium"}


def test_accepted_legacy_default_task_runs_with_high(tmp_path, fake_omp_binary):
    settings, meeting_id, task = _antigravity_task(tmp_path, "legacy-1")
    with sqlite3.connect(database_path(settings.data_dir)) as connection:
        connection.execute(
            "UPDATE tasks SET notes_reasoning_effort = 'default' WHERE kind = 'notes'"
        )
    task = {**task, "notes_reasoning_effort": "default"}

    _run_with_worker(settings, task, "owner-legacy-1")

    assert {call["level"] for call in _omp_calls(fake_omp_binary)} == {"high"}


@pytest.mark.anyio
async def test_antigravity_confirmed_level_is_compared_after_legacy_default_is_normalized(
    tmp_path, monkeypatch
):
    settings = _settings(tmp_path)
    async with _api(settings, monkeypatch) as api:
        meeting_id, generation_id, _ = seed_completed_transcript(settings.data_dir)
        runs = f"/api/meetings/{meeting_id}/notes-runs"
        _connect_antigravity(api.app.state.antigravity_provider)
        provider = api.app.state.antigravity_provider
        provider.save_notes_preference(PINNED_MODEL, "medium")

        stale = await api.post(
            runs,
            api.run_payload(
                meeting_id,
                generation_id,
                "stale-level-1",
                provider="antigravity",
                expected_reasoning_effort="default",  # legacy body, saved level is medium
            ),
        )
        assert stale.status_code == 409, stale.json()
        assert stale.json()["detail"]["code"] == "notes_preference_changed"

        provider.save_notes_preference(PINNED_MODEL, "high")
        legacy = await api.post(
            runs,
            api.run_payload(
                meeting_id,
                generation_id,
                "legacy-level-1",
                provider="antigravity",
                expected_reasoning_effort="default",
            ),
        )
        assert legacy.status_code == 202
        assert legacy.json()["reasoning_effort"] == "high"


# --- long transcripts, exhausted repair and lost connections, end to end -----------------------


def _add_segments(data_dir, generation_id, count, *, first_sequence=2):
    """Append short, distinct segments after the two seeded ones, in transcript order."""
    rows = [
        (
            uuid.uuid4().hex,
            generation_id,
            sequence,
            5.0 + (sequence - first_sequence) * 0.025,
            5.01 + (sequence - first_sequence) * 0.025,
            f"Long-source segment {sequence}: review the proposal and unresolved question.",
            "SPEAKER_00",
            "SPEAKER_00",
            0,
        )
        for sequence in range(first_sequence, first_sequence + count)
    ]
    with sqlite3.connect(database_path(data_dir)) as connection:
        connection.executemany(
            "INSERT INTO transcript_segments(id, generation_id, sequence, start_seconds, "
            "end_seconds, text, original_speaker_id, speaker_id, uncertain) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )


def _stored_coverage(data_dir, revision_id):
    with sqlite3.connect(database_path(data_dir)) as connection:
        stored = connection.execute(
            "SELECT coverage_json FROM notes_revisions WHERE id = ?", (revision_id,)
        ).fetchone()[0]
    return json.loads(stored)


@pytest.mark.parametrize("provider_id", ["chatgpt", "antigravity"])
def test_long_transcript_run_keeps_a_coverage_manifest_with_the_revision(
    tmp_path, monkeypatch, capsys, provider_id
):
    data_dir = tmp_path / "data"
    meeting_id, generation_id, _ = seed_completed_transcript(data_dir)
    _add_segments(data_dir, generation_id, 998)
    rows_before = _transcript_rows(data_dir)
    pair = ProviderPair(tmp_path, monkeypatch)
    task = _accept(
        data_dir, pair.provider(provider_id), provider_id, "long-1", meeting_id, generation_id
    )
    claim_next_task(data_dir, owner_id="owner-long")

    event, revision = _run_notes(data_dir, capsys, task["id"], "owner-long")

    assert event["type"] == "result"
    calls = pair.calls(provider_id)
    manifest = _stored_coverage(data_dir, revision["id"])
    chunks = manifest["chunks"]
    assert manifest["segment_count"] == 1_000 and len(chunks) > 2
    # Every planned request has the call that answered it, and the owned ranges tile the source.
    assert [chunk["provider_call_index"] for chunk in chunks] == list(range(1, len(chunks) + 1))
    assert len(calls) == len(chunks) + 1
    assert chunks[0]["owned"][0] == 0 and chunks[-1]["owned"][1] == 1_000
    assert all(left["owned"][1] == right["owned"][0] for left, right in pairwise(chunks))
    assert all(chunk["overlap"] is not None for chunk in chunks[1:])
    assert (manifest["provider_calls"], manifest["output_recoveries"]) == (len(calls), 0)
    assert _transcript_rows(data_dir) == rows_before


def _second_notes_task_after_one_revision(tmp_path):
    """Publish one revision, then run a second chunked task through the real worker path."""
    settings = Settings(data_dir=tmp_path / "data", auth_dir=tmp_path / "auth")
    meeting_id, generation_id, _ = seed_completed_transcript(settings.data_dir)
    _lengthen_transcript(settings.data_dir)
    provider = _antigravity(tmp_path, FakeOmp())
    first = _accept(settings.data_dir, provider, "antigravity", "old", meeting_id, generation_id)
    claim_next_task(settings.data_dir, owner_id="owner-old")
    old_revision = publish_notes_revision(
        settings.data_dir,
        first["id"],
        owner_id="owner-old",
        markdown="# Old notes\n\nKeep this saved revision.\n",
        elapsed_seconds=1.0,
    )
    assert old_revision is not None
    second = _accept(settings.data_dir, provider, "antigravity", "new", meeting_id, generation_id)
    task = claim_next_task(settings.data_dir, owner_id="owner-new")
    assert task is not None and task["id"] == second["id"]
    return settings, meeting_id, task, old_revision


def test_exhausted_repair_publishes_no_partial_revision_and_keeps_old_notes_and_transcript(
    tmp_path, fake_omp_binary
):
    settings, meeting_id, task, old_revision = _second_notes_task_after_one_revision(tmp_path)
    (fake_omp_binary / "mode").write_text("fail_after_first")
    rows_before = _transcript_rows(settings.data_dir)

    _run_with_worker(settings, task, "owner-new")

    state = notes_state(settings.data_dir, meeting_id)
    assert state["task"]["status"] == "failed"
    assert state["task"]["error_code"] == "notes_invalid_output"
    # Chunk one was valid; chunk two and its single repair were not. Nothing was published.
    assert len(_omp_calls(fake_omp_binary)) == 3
    assert [item["id"] for item in state["revisions"]] == [old_revision["id"]]
    assert get_note(settings.data_dir, old_revision["id"])["markdown"] == (
        "# Old notes\n\nKeep this saved revision.\n"
    )
    assert _transcript_rows(settings.data_dir) == rows_before
    assert state["source_locked"] is False


def test_network_error_after_the_prompt_was_sent_is_not_resent_and_publishes_nothing(
    tmp_path, fake_omp_binary
):
    settings, meeting_id, task, old_revision = _second_notes_task_after_one_revision(tmp_path)
    (fake_omp_binary / "mode").write_text("network")

    _run_with_worker(settings, task, "owner-new")

    state = notes_state(settings.data_dir, meeting_id)
    assert state["task"]["status"] == "failed"
    assert [item["id"] for item in state["revisions"]] == [old_revision["id"]]
    # One OMP process, one prompt: the app did not resend after the connection dropped. OMP's own
    # retry is off in its config (test_runner_contract_is_isolated_and_disables_agent_surfaces).
    assert len(_omp_calls(fake_omp_binary)) == 1
