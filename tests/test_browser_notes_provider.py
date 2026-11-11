from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import pytest
from notes_helpers import StubNotesProvider, seed_completed_transcript
from playwright.sync_api import expect
from test_browser_history import live as live
from test_browser_history import signed_in as signed_in

from asr_studio import notes_routes
from asr_studio.notes import accept_notes_task, get_note, publish_notes_revision, update_note
from asr_studio.storage import database_path
from asr_studio.tasks import claim_next_task, fail_task

pytestmark = pytest.mark.browser

CHATGPT_MODEL = "gpt-6.1-sol"
ANTIGRAVITY_MODEL = "gemini-3-pro-high"
ANTIGRAVITY_OTHER_MODEL = "claude-sonnet-test"
CHATGPT_CATALOG = [
    {
        "id": CHATGPT_MODEL,
        "display_name": "GPT 6.1 Sol",
        "reasoning_efforts": ["low", "medium", "high", "xhigh", "max"],
    },
    {"id": "gpt-6-luna", "display_name": "GPT 6 Luna", "reasoning_efforts": ["none", "low"]},
]
ANTIGRAVITY_CATALOG = [
    {"id": ANTIGRAVITY_MODEL, "display_name": "Gemini 3 Pro High"},
    {"id": ANTIGRAVITY_OTHER_MODEL, "display_name": "Claude Sonnet Test"},
]


class PinnedStub(StubNotesProvider):
    def __init__(self, provider: str, effort: str = "high") -> None:
        super().__init__()
        self.provider = provider
        self.effort = effort

    def notes_task_pin(self) -> dict[str, object]:
        if self.provider == "antigravity":
            return {
                "provider": "antigravity",
                "connection_version": 3,
                "model": ANTIGRAVITY_MODEL,
                "reasoning_effort": self.effort,
            }
        return super().notes_task_pin()


@pytest.fixture
def fake_notes_provider(monkeypatch):
    original_accept = notes_routes.accept_notes_task

    def accept_with_stub(data_dir, *, provider, provider_id="chatgpt", **kwargs):
        return original_accept(
            data_dir,
            provider=PinnedStub(
                provider_id,
                provider.saved_notes_effort() if provider_id == "antigravity" else "high",
            ),
            provider_id=provider_id,
            **kwargs,
        )

    monkeypatch.setattr(notes_routes, "accept_notes_task", accept_with_stub)
    monkeypatch.setattr(notes_routes, "worker_is_healthy", lambda _path, *, role=None: True)


def mock_providers(
    page,
    live,
    *,
    chatgpt="connected",
    antigravity="connected",
    chatgpt_catalog=CHATGPT_CATALOG,
    antigravity_catalog=ANTIGRAVITY_CATALOG,
):
    def providers(route):
        route.fulfill(
            json={
                "providers": [
                    {"id": "chatgpt", "status": chatgpt, "connection_version": 7},
                    {"id": "antigravity", "status": antigravity, "connection_version": 3},
                ]
            }
        )

    page.route(f"{live.url}/api/providers", providers)
    page.route(
        f"{live.url}/api/providers/chatgpt/models",
        lambda route: route.fulfill(json={"models": chatgpt_catalog, "selected_model": None}),
    )
    page.route(
        f"{live.url}/api/providers/antigravity/models",
        lambda route: route.fulfill(json={"models": antigravity_catalog, "selected_model": None}),
    )


def save_preference(page, live, provider, model, effort="default"):
    # The API requires a same-origin request, so call it from the signed-in page.
    status = page.evaluate(
        """async (body) => (await fetch("/api/notes-preferences", {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        })).status""",
        {"provider": provider, "model": model, "reasoning_effort": effort},
    )
    assert status == 200


def accept(data_dir: Path, meeting_id, generation_id, *, request_id, provider="chatgpt"):
    return accept_notes_task(
        data_dir,
        provider=PinnedStub(provider),
        provider_id=provider,
        meeting_id=meeting_id,
        request_id=request_id,
        expected_generation_id=generation_id,
        expected_source_version=1,
        expected_metadata_version=1,
        worker_ready=True,
    )


def claim(data_dir: Path, task_id: str) -> str:
    owner_id = f"browser-provider-worker-{task_id}"
    claimed = claim_next_task(data_dir, owner_id=owner_id)
    assert claimed is not None and claimed["id"] == task_id
    return owner_id


def complete(data_dir: Path, task_id: str, markdown: str) -> dict:
    owner_id = claim(data_dir, task_id)
    revision = publish_notes_revision(
        data_dir, task_id, owner_id=owner_id, markdown=markdown, elapsed_seconds=0.1
    )
    assert revision is not None
    return revision


def fail(data_dir: Path, task_id: str, code: str) -> None:
    owner_id = claim(data_dir, task_id)
    assert fail_task(
        data_dir,
        task_id,
        owner_id=owner_id,
        error_code=code,
        message="Provider text from the server.",
        elapsed_seconds=0.1,
    )


def task_status(data_dir: Path, task_id: str) -> str:
    with sqlite3.connect(database_path(data_dir)) as connection:
        return connection.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()[0]


def capture_runs(page) -> list[dict]:
    bodies: list[dict] = []
    page.on(
        "request",
        lambda request: (
            bodies.append(json.loads(request.post_data or "{}"))
            if request.method == "POST" and request.url.endswith("/notes-runs")
            else None
        ),
    )
    return bodies


@pytest.mark.parametrize(
    ("provider", "model", "label", "model_label"),
    [
        ("antigravity", ANTIGRAVITY_OTHER_MODEL, "Google Antigravity", "Claude Sonnet Test"),
        ("chatgpt", CHATGPT_MODEL, "ChatGPT", "GPT 6.1 Sol"),
    ],
)
def test_saved_provider_and_model_are_preselected(
    live, signed_in, provider, model, label, model_label
):
    _context, page = signed_in()
    meeting_id, _generation, _ = seed_completed_transcript(live.settings.data_dir)
    save_preference(page, live, "chatgpt", CHATGPT_MODEL, "high")
    save_preference(page, live, "antigravity", ANTIGRAVITY_MODEL)
    save_preference(page, live, provider, model, "high" if provider == "chatgpt" else "medium")
    mock_providers(page, live)
    page.goto(f"{live.url}/meetings/{meeting_id}#notes")
    expect(page.locator("#notes-provider")).to_have_value(provider)
    expect(page.locator("#notes-model")).to_have_value(model)
    expect(page.locator("#notes-run-plan")).to_contain_text(f"{label} · {model_label}")
    expect(page.locator("#notes-start")).to_have_text(
        f"Tạo notes · English · {label} {model_label}"
    )
    expect(page.locator("#notes-start")).to_be_enabled()
    expect(page.locator("#notes-content-policy")).to_contain_text(f"tới {label}")
    if provider == "chatgpt":
        expect(page.locator("#notes-run-plan")).to_contain_text("effort high")
    else:
        expect(page.locator("#notes-run-plan")).to_contain_text(
            f"{label} · {model_label} · mức suy nghĩ Medium."
        )


def test_missing_saved_model_requires_an_explicit_choice(live, signed_in):
    _context, page = signed_in()
    meeting_id, _generation, _ = seed_completed_transcript(live.settings.data_dir)
    save_preference(page, live, "antigravity", "model-that-was-removed")
    mock_providers(page, live)
    runs = capture_runs(page)
    page.goto(f"{live.url}/meetings/{meeting_id}#notes")
    expect(page.locator("#notes-provider")).to_have_value("antigravity")
    expect(page.locator("#notes-model")).to_have_value("")
    expect(page.locator("#notes-run-plan")).to_contain_text("model-that-was-removed")
    expect(page.locator("#notes-run-plan")).to_contain_text("không tự chọn thay")
    expect(page.locator("#notes-start")).to_have_text("Chọn hoặc đổi model để tạo notes.")
    # The provider is connected; the action opens the existing choice surface and focuses its model.
    page.locator("#notes-start").click()
    expect(page.locator("#notes-choice")).to_be_visible()
    expect(page.locator("#notes-model")).to_be_focused()
    page.locator("#notes-model").select_option(ANTIGRAVITY_MODEL)
    expect(page.locator("#notes-preference-status")).to_contain_text("lần chạy tiếp theo")
    expect(page.locator("#notes-start")).to_have_text(
        "Tạo notes · English · Google Antigravity Gemini 3 Pro High"
    )
    expect(page.locator("#notes-start")).to_be_enabled()
    assert runs == []


def test_catalog_blocked_by_active_provider_keeps_saved_model_with_reason(live, signed_in):
    _context, page = signed_in()
    meeting_id, _generation, _ = seed_completed_transcript(live.settings.data_dir)
    save_preference(page, live, "antigravity", ANTIGRAVITY_MODEL)
    mock_providers(page, live)
    page.route(
        f"{live.url}/api/providers/antigravity/models",
        lambda route: route.fulfill(
            status=409, json={"detail": {"code": "provider_in_use", "message": "busy"}}
        ),
    )
    page.goto(f"{live.url}/meetings/{meeting_id}#notes")
    expect(page.locator("#notes-model")).to_have_value(ANTIGRAVITY_MODEL)
    expect(page.locator("#notes-model")).to_be_disabled()
    expect(page.locator("#notes-run-plan")).to_contain_text("đang chạy notes")


def test_antigravity_run_produces_labelled_revision_and_regeneration_keeps_edits(
    live, signed_in, fake_notes_provider
):
    _context, page = signed_in()
    meeting_id, generation_id, _ = seed_completed_transcript(live.settings.data_dir)
    save_preference(page, live, "chatgpt", CHATGPT_MODEL, "high")
    save_preference(page, live, "antigravity", ANTIGRAVITY_MODEL)
    mock_providers(page, live)
    runs = capture_runs(page)
    page.goto(f"{live.url}/meetings/{meeting_id}#notes")
    expect(page.locator("#notes-provider")).to_have_value("antigravity")
    with page.expect_response(
        lambda response: response.url.endswith("/notes-runs") and response.request.method == "POST"
    ) as accepted_response:
        page.locator("#notes-start").click()
    assert accepted_response.value.status == 202
    accepted = accepted_response.value.json()
    assert runs[0]["provider"] == "antigravity"
    assert accepted["provider"] == "antigravity" and accepted["model"] == ANTIGRAVITY_MODEL
    expect(page.locator("#notes-task-provider-model")).to_have_text(
        f"Google Antigravity · {ANTIGRAVITY_MODEL} · mức suy nghĩ High"
    )

    first = complete(live.settings.data_dir, accepted["id"], "# First\n\nAntigravity notes.\n")
    expect(page.locator("#notes-saved-work")).to_be_visible(timeout=10_000)
    expect(page.locator("#notes-revision-meta")).to_contain_text(
        f"Google Antigravity · {ANTIGRAVITY_MODEL}"
    )
    expect(page.locator("#notes-start")).to_have_text(
        "Tạo lại notes · English · Google Antigravity Gemini 3 Pro High"
    )

    edited = "# First\n\nAn operator edit that must survive.\n"
    update_note(live.settings.data_dir, first["id"], expected_edit_version=1, markdown=edited)

    page.locator("#notes-change").click()
    page.locator("#notes-provider").select_option("chatgpt")
    expect(page.locator("#notes-start")).to_have_text(
        "Tạo lại notes · English · ChatGPT GPT 6.1 Sol"
    )
    with page.expect_response(
        lambda response: response.url.endswith("/notes-runs") and response.request.method == "POST"
    ) as second_response:
        page.locator("#notes-start").click()
    second_task = second_response.value.json()
    assert runs[1]["provider"] == "chatgpt" and second_task["provider"] == "chatgpt"
    second = complete(live.settings.data_dir, second_task["id"], "# Second\n\nChatGPT notes.\n")
    expect(page.locator("#notes-revision-selector option")).to_have_count(2, timeout=10_000)
    options = page.locator("#notes-revision-selector option").all_text_contents()
    assert any("Google Antigravity" in text for text in options)
    assert any("ChatGPT" in text for text in options)
    page.locator("#notes-revision-selector").select_option(first["id"])
    expect(page.locator("#notes-markdown")).to_have_value(edited)
    expect(page.locator("#notes-revision-meta")).to_contain_text("Google Antigravity")
    assert get_note(live.settings.data_dir, first["id"])["markdown"] == edited
    assert second["id"] != first["id"]


def test_preference_change_during_active_run_applies_to_next_run(
    live, signed_in, fake_notes_provider
):
    _context, page = signed_in()
    meeting_id, generation_id, _ = seed_completed_transcript(live.settings.data_dir)
    save_preference(page, live, "chatgpt", CHATGPT_MODEL, "high")
    save_preference(page, live, "antigravity", ANTIGRAVITY_MODEL)
    active = accept(
        live.settings.data_dir,
        meeting_id,
        generation_id,
        request_id="browser-provider-active",
        provider="antigravity",
    )
    owner_id = claim(live.settings.data_dir, active["id"])
    mock_providers(page, live)
    page.goto(f"{live.url}/meetings/{meeting_id}#notes")
    expect(page.locator("#notes-task-provider-model")).to_have_text(
        f"Google Antigravity · {ANTIGRAVITY_MODEL} · mức suy nghĩ High"
    )
    expect(page.locator("#notes-start")).to_be_disabled()
    expect(page.locator("#notes-provider")).to_be_enabled()
    page.locator("#notes-change").click()
    page.locator("#notes-provider").select_option("chatgpt")
    expect(page.locator("#notes-preference-status")).to_contain_text(
        "chỉ áp dụng cho lần chạy tiếp theo"
    )
    expect(page.locator("#notes-preference-status")).to_contain_text(
        f"Google Antigravity · {ANTIGRAVITY_MODEL}"
    )
    expect(page.locator("#notes-task-provider-model")).to_have_text(
        f"Google Antigravity · {ANTIGRAVITY_MODEL} · mức suy nghĩ High"
    )
    expect(page.locator("#notes-status")).to_contain_text("Google Antigravity đang tạo notes")
    page.locator("#notes-model").select_option("gpt-6-luna")
    expect(page.locator("#notes-task-provider-model")).to_have_text(
        f"Google Antigravity · {ANTIGRAVITY_MODEL} · mức suy nghĩ High"
    )
    assert active["model"] == ANTIGRAVITY_MODEL
    assert fail_task(
        live.settings.data_dir,
        active["id"],
        owner_id=owner_id,
        error_code="provider_quota",
        message="Provider text from the server.",
        elapsed_seconds=0.1,
    )
    expect(
        page.locator('#notes-steps > li[data-step-state="failed"]:not([hidden])')
    ).to_contain_text("Thất bại", timeout=10_000)
    expect(page.locator("#notes-preference-status")).not_to_contain_text("đang chạy giữ")


@pytest.mark.parametrize(
    ("failed_provider", "code", "other", "message"),
    [
        ("antigravity", "provider_quota", "chatgpt", "Google Antigravity đã hết hạn mức"),
        ("chatgpt", "provider_disconnected", "antigravity", "ChatGPT chưa được kết nối"),
        (
            "antigravity",
            "provider_invalid_response",
            "chatgpt",
            "Google Antigravity dừng mà không trả về kết quả dùng được",
        ),
    ],
)
def test_failed_run_is_reported_and_new_run_can_use_other_provider(
    live, signed_in, fake_notes_provider, failed_provider, code, other, message
):
    _context, page = signed_in()
    meeting_id, generation_id, _ = seed_completed_transcript(live.settings.data_dir)
    save_preference(page, live, "chatgpt", CHATGPT_MODEL, "high")
    save_preference(page, live, "antigravity", ANTIGRAVITY_MODEL)
    save_preference(
        page,
        live,
        failed_provider,
        CHATGPT_MODEL if failed_provider == "chatgpt" else ANTIGRAVITY_MODEL,
        "high" if failed_provider == "chatgpt" else "default",
    )
    failed = accept(
        live.settings.data_dir,
        meeting_id,
        generation_id,
        request_id="browser-provider-failed",
        provider=failed_provider,
    )
    mock_providers(page, live)
    runs = capture_runs(page)
    page.goto(f"{live.url}/meetings/{meeting_id}#notes")
    fail(live.settings.data_dir, failed["id"], code)
    expect(
        page.locator('#notes-steps > li[data-step-state="failed"]:not([hidden])')
    ).to_contain_text("Thất bại", timeout=10_000)
    expect(page.locator("#notes-status")).to_contain_text(message)
    expect(page.locator("#notes-status")).to_contain_text("không tự đổi")
    expect(page.locator("#notes-status")).to_contain_text("lần chạy mới")
    expect(page.locator("#notes-provider")).to_be_enabled()
    expect(page.locator("#notes-model")).to_be_enabled()

    page.locator("#notes-change").click()
    page.locator("#notes-provider").select_option(other)
    expect(page.locator("#notes-start")).to_be_enabled()
    with page.expect_response(
        lambda response: response.url.endswith("/notes-runs") and response.request.method == "POST"
    ) as next_response:
        page.locator("#notes-start").click()
    assert next_response.value.status == 202
    new_task = next_response.value.json()
    assert runs[-1]["provider"] == other and new_task["provider"] == other
    assert new_task["id"] != failed["id"]
    provider_name = "ChatGPT" if other == "chatgpt" else "Google Antigravity"
    expect(page.locator("#notes-task-provider-model")).to_have_text(
        re.compile(rf"^{provider_name} · {re.escape(new_task['model'])}")
    )
    assert task_status(live.settings.data_dir, failed["id"]) == "failed"
    expect(page.locator("#notes-saved-work")).to_be_hidden()


def test_start_rejection_names_the_chosen_provider(live, signed_in):
    _context, page = signed_in()
    meeting_id, _generation, _ = seed_completed_transcript(live.settings.data_dir)
    save_preference(page, live, "antigravity", ANTIGRAVITY_MODEL)
    mock_providers(page, live)
    page.route(
        f"{live.url}/api/meetings/{meeting_id}/notes-runs",
        lambda route: route.fulfill(
            status=409, json={"detail": {"code": "reconnect_required", "message": "x"}}
        ),
    )
    page.goto(f"{live.url}/meetings/{meeting_id}#notes")
    page.locator("#notes-start").click()
    expect(page.locator("#notes-error-message")).to_contain_text(
        "Google Antigravity cần được kết nối lại"
    )
    expect(page.locator("#notes-error-message")).to_contain_text("không tự đổi")
    expect(page.locator("#notes-start")).to_have_text(
        "Tạo notes · English · Google Antigravity Gemini 3 Pro High"
    )
    expect(page.locator("#notes-start")).to_be_enabled()


def test_pending_retry_resends_the_stored_provider(live, signed_in, fake_notes_provider):
    _context, page = signed_in()
    meeting_id, _generation, _ = seed_completed_transcript(live.settings.data_dir)
    save_preference(page, live, "chatgpt", CHATGPT_MODEL, "high")
    save_preference(page, live, "antigravity", ANTIGRAVITY_MODEL)
    mock_providers(page, live)
    bodies: list[dict] = []

    def notes_request(route):
        bodies.append(json.loads(route.request.post_data or "{}"))
        if len(bodies) == 1:
            route.fulfill(status=503, json={"detail": {"code": "temporary", "message": "Retry."}})
        else:
            route.continue_()

    page.route(f"{live.url}/api/meetings/{meeting_id}/notes-runs", notes_request)
    page.goto(f"{live.url}/meetings/{meeting_id}#notes")
    expect(page.locator("#notes-provider")).to_have_value("antigravity")
    page.locator("#notes-start").click()
    expect(page.locator("#notes-start")).to_have_text("Xác minh yêu cầu notes")
    expect(page.locator("#notes-provider")).to_be_disabled()
    page.reload()
    expect(page.locator("#notes-provider")).to_have_value("antigravity")
    expect(page.locator("#notes-start")).to_have_text("Xác minh yêu cầu notes")
    with page.expect_response(
        lambda response: response.url.endswith("/notes-runs") and response.request.method == "POST"
    ) as retry:
        page.locator("#notes-start").click()
    assert retry.value.status == 202
    assert bodies[0]["provider"] == "antigravity"
    assert bodies[1] == bodies[0]
    assert retry.value.json()["provider"] == "antigravity"


def test_stored_pending_request_without_provider_means_chatgpt(live, signed_in):
    _context, page = signed_in()
    meeting_id, generation_id, _ = seed_completed_transcript(live.settings.data_dir)
    save_preference(page, live, "antigravity", ANTIGRAVITY_MODEL)
    mock_providers(page, live)
    bodies: list[dict] = []

    def notes_request(route):
        bodies.append(json.loads(route.request.post_data or "{}"))
        route.fulfill(status=503, json={"detail": {"code": "temporary", "message": "Retry."}})

    page.route(f"{live.url}/api/meetings/{meeting_id}/notes-runs", notes_request)
    page.goto(f"{live.url}/meetings/{meeting_id}#notes")
    stored = {
        "request_id": "old-pending-request-1",
        "output_language": "en",
        "expected_generation_id": generation_id,
        "expected_source_version": 1,
        "expected_metadata_version": 1,
    }
    page.evaluate(
        "([key, value]) => localStorage.setItem(key, value)",
        [f"asr-studio:notes-request-v2:{meeting_id}", json.dumps(stored)],
    )
    page.reload()
    expect(page.locator("#notes-start")).to_have_text("Xác minh yêu cầu notes")
    expect(page.locator("#notes-provider")).to_have_value("chatgpt")
    with page.expect_request(lambda r: r.url.endswith("/notes-runs")):
        page.locator("#notes-start").click()
    assert bodies and bodies[0] == {**stored, "provider": "chatgpt"}


def announce_choice_change(page):
    page.evaluate(
        "async () => (await import('/assets/channel.js')).providerChoice.dispatch("
        "{ type: 'channel/provider-choice-changed' })"
    )


def test_model_saved_elsewhere_refreshes_the_open_notes_panel(live, signed_in, fake_notes_provider):
    _context, page = signed_in()
    meeting_id, _generation, _ = seed_completed_transcript(live.settings.data_dir)
    save_preference(page, live, "chatgpt", CHATGPT_MODEL, "high")
    mock_providers(page, live)
    runs = capture_runs(page)
    page.goto(f"{live.url}/meetings/{meeting_id}#notes")
    expect(page.locator("#notes-model")).to_have_value(CHATGPT_MODEL)
    save_preference(page, live, "chatgpt", "gpt-6-luna", "low")
    announce_choice_change(page)
    expect(page.locator("#notes-model")).to_have_value("gpt-6-luna")
    expect(page.locator("#notes-run-plan")).to_contain_text("GPT 6 Luna")
    with page.expect_response(
        lambda response: response.url.endswith("/notes-runs") and response.request.method == "POST"
    ):
        page.locator("#notes-start").click()
    assert runs[0]["expected_model"] == "gpt-6-luna"
    assert runs[0]["expected_reasoning_effort"] == "low"


def test_model_saved_in_the_connections_dialog_refreshes_the_open_notes_panel(
    live, signed_in, fake_notes_provider
):
    _context, page = signed_in()
    meeting_id, _generation, _ = seed_completed_transcript(live.settings.data_dir)
    save_preference(page, live, "chatgpt", CHATGPT_MODEL, "high")
    mock_providers(page, live)
    page.goto(f"{live.url}/meetings/{meeting_id}#notes")
    expect(page.locator("#notes-model")).to_have_value(CHATGPT_MODEL)
    page.locator("#launcher-chatgpt").click()
    expect(page.locator("#connections-dialog")).to_be_visible()
    expect(page.locator("#connection-model")).to_have_value(CHATGPT_MODEL)
    page.locator("#connection-model").select_option("gpt-6-luna")
    page.locator("#connection-model-save").click()
    expect(page.locator("#connection-model-status")).to_contain_text("Luna")
    page.locator("#connections-close").click()
    expect(page.locator("#notes-model")).to_have_value("gpt-6-luna")


def test_antigravity_run_confirms_model_and_thinking_level(live, signed_in, fake_notes_provider):
    _context, page = signed_in()
    meeting_id, _generation, _ = seed_completed_transcript(live.settings.data_dir)
    save_preference(page, live, "antigravity", ANTIGRAVITY_MODEL, "medium")
    mock_providers(page, live)
    runs = capture_runs(page)
    page.goto(f"{live.url}/meetings/{meeting_id}#notes")
    with page.expect_response(lambda r: r.url.endswith("/notes-runs")):
        page.locator("#notes-start").click()
    assert runs[0]["expected_model"] == ANTIGRAVITY_MODEL
    assert runs[0]["expected_reasoning_effort"] == "medium"
    expect(page.locator("#notes-task-provider-model")).to_contain_text("mức suy nghĩ Medium")


def test_preference_changed_conflict_reloads_the_choice_without_retry(live, signed_in):
    _context, page = signed_in()
    meeting_id, _generation, _ = seed_completed_transcript(live.settings.data_dir)
    save_preference(page, live, "chatgpt", CHATGPT_MODEL, "high")
    mock_providers(page, live)
    bodies: list[dict] = []

    def conflict(route):
        bodies.append(json.loads(route.request.post_data or "{}"))
        # The operator saved another model in the meantime; the next GET shows it.
        save_preference(page, live, "chatgpt", "gpt-6-luna", "low")
        route.fulfill(
            status=409, json={"detail": {"code": "notes_preference_changed", "message": "x"}}
        )

    page.route(f"{live.url}/api/meetings/{meeting_id}/notes-runs", conflict)
    page.goto(f"{live.url}/meetings/{meeting_id}#notes")
    expect(page.locator("#notes-model")).to_have_value(CHATGPT_MODEL)
    page.locator("#notes-start").click()
    expect(page.locator("#notes-error-message")).to_contain_text("xác nhận lại")
    expect(page.locator("#notes-model")).to_have_value("gpt-6-luna")
    expect(page.locator("#notes-start")).to_have_text("Tạo notes · English · ChatGPT GPT 6 Luna")
    page.wait_for_timeout(300)
    assert len(bodies) == 1


def test_failed_preference_load_keeps_start_disabled_until_reloaded(live, signed_in):
    _context, page = signed_in()
    meeting_id, _generation, _ = seed_completed_transcript(live.settings.data_dir)
    save_preference(page, live, "chatgpt", CHATGPT_MODEL, "high")
    mock_providers(page, live)
    failing = {"on": True}

    def preferences(route):
        if failing["on"]:
            route.fulfill(status=500, json={"detail": {"code": "x", "message": "x"}})
        else:
            route.fallback()

    page.route(f"{live.url}/api/notes-preferences", preferences)
    page.goto(f"{live.url}/meetings/{meeting_id}#notes")
    expect(page.locator("#notes-run-plan")).to_contain_text("Chưa đọc được lựa chọn")
    expect(page.locator("#notes-start")).to_be_disabled()
    expect(page.locator("#notes-choice-reload")).to_be_visible()
    failing["on"] = False
    # The next notes read may already have retried on its own; the control is the manual path.
    page.evaluate("document.querySelector('#notes-choice-reload').click()")
    expect(page.locator("#notes-start")).to_be_enabled()
    expect(page.locator("#notes-choice-reload")).to_be_hidden()
    expect(page.locator("#notes-model")).to_have_value(CHATGPT_MODEL)


def test_pending_retry_resends_stored_body_while_preferences_fail(
    live, signed_in, fake_notes_provider
):
    _context, page = signed_in()
    meeting_id, _generation, _ = seed_completed_transcript(live.settings.data_dir)
    save_preference(page, live, "chatgpt", CHATGPT_MODEL, "high")
    mock_providers(page, live)
    bodies: list[dict] = []

    def notes_request(route):
        bodies.append(json.loads(route.request.post_data or "{}"))
        if len(bodies) == 1:
            route.fulfill(status=503, json={"detail": {"code": "temporary", "message": "Retry."}})
        else:
            route.continue_()

    page.route(f"{live.url}/api/meetings/{meeting_id}/notes-runs", notes_request)
    page.goto(f"{live.url}/meetings/{meeting_id}#notes")
    expect(page.locator("#notes-start")).to_be_enabled()
    page.locator("#notes-start").click()
    expect(page.locator("#notes-start")).to_have_text("Xác minh yêu cầu notes")
    assert bodies[0]["expected_model"] == CHATGPT_MODEL
    page.route(
        f"{live.url}/api/notes-preferences",
        lambda route: route.fulfill(status=500, json={"detail": {"code": "x", "message": "x"}}),
    )
    page.reload()
    expect(page.locator("#notes-start")).to_have_text("Xác minh yêu cầu notes")
    expect(page.locator("#notes-start")).to_be_enabled()
    with page.expect_response(lambda r: r.url.endswith("/notes-runs")) as retry:
        page.locator("#notes-start").click()
    assert retry.value.status == 202
    assert bodies[1] == bodies[0]


def test_complete_pending_retry_survives_choice_reload_between_pointer_events(
    live, signed_in, fake_notes_provider
):
    _context, page = signed_in()
    meeting_id, _generation, _ = seed_completed_transcript(live.settings.data_dir)
    save_preference(page, live, "chatgpt", CHATGPT_MODEL, "high")
    mock_providers(page, live)
    bodies: list[dict] = []

    def notes_request(route):
        bodies.append(json.loads(route.request.post_data or "{}"))
        if len(bodies) == 1:
            route.fulfill(status=503, json={"detail": {"code": "temporary", "message": "Retry."}})
        else:
            route.continue_()

    page.route(f"{live.url}/api/meetings/{meeting_id}/notes-runs", notes_request)
    page.goto(f"{live.url}/meetings/{meeting_id}#notes")
    button = page.locator("#notes-start")
    expect(button).to_be_enabled()
    button.click()
    expect(button).to_have_text("Xác minh yêu cầu notes")
    assert bodies[0]["expected_model"] == CHATGPT_MODEL
    assert bodies[0]["expected_reasoning_effort"] == "high"

    page.route(
        f"{live.url}/api/notes-preferences",
        lambda route: route.fulfill(status=500, json={"detail": {"code": "x", "message": "x"}}),
    )
    page.reload()
    expect(button).to_have_text("Xác minh yêu cầu notes")
    expect(button).to_be_enabled()

    held = []
    page.route(f"{live.url}/api/notes-preferences", lambda route: held.append(route))
    bounds = button.bounding_box()
    page.mouse.move(bounds["x"] + bounds["width"] / 2, bounds["y"] + bounds["height"] / 2)
    page.mouse.down()
    with page.expect_request(lambda request: request.url.endswith("/api/notes-preferences")):
        announce_choice_change(page)
    try:
        with page.expect_response(
            lambda response: (
                response.url.endswith("/notes-runs") and response.request.method == "POST"
            ),
            timeout=2000,
        ) as retry:
            page.mouse.up()
        assert retry.value.status == 202
        assert len(bodies) == 2
        assert bodies[1] == bodies[0]
        assert bodies[1]["request_id"] == bodies[0]["request_id"]
        assert bodies[1]["expected_model"] == CHATGPT_MODEL
        assert bodies[1]["expected_reasoning_effort"] == "high"
    finally:
        for route in held:
            route.fulfill(status=500, json={"detail": {"code": "x", "message": "x"}})


def test_incomplete_pending_request_waits_for_choice_loading_before_completion(
    live, signed_in, fake_notes_provider
):
    _context, page = signed_in()
    meeting_id, generation_id, _ = seed_completed_transcript(live.settings.data_dir)
    save_preference(page, live, "chatgpt", CHATGPT_MODEL, "high")
    mock_providers(page, live)
    bodies: list[dict] = []

    def notes_request(route):
        bodies.append(json.loads(route.request.post_data or "{}"))
        route.continue_()

    page.route(f"{live.url}/api/meetings/{meeting_id}/notes-runs", notes_request)
    stored = {"request_id": "old-incomplete-request-2", "output_language": "en"}
    # Written before the page script runs, so the first notes load always sees the stored request.
    key = json.dumps(f"asr-studio:notes-request-v2:{meeting_id}")
    page.add_init_script(f"localStorage.setItem({key}, {json.dumps(json.dumps(stored))})")
    page.goto(f"{live.url}/meetings/{meeting_id}#notes")

    held = []
    page.route(f"{live.url}/api/notes-preferences", lambda route: held.append(route))
    button = page.locator("#notes-start")
    with page.expect_request(lambda request: request.url.endswith("/api/notes-preferences")):
        announce_choice_change(page)
    try:
        expect(button).to_have_text("Xác minh yêu cầu notes")
        expect(button).to_be_disabled()
    finally:
        for route in held:
            route.continue_()
        held.clear()

    expect(button).to_be_enabled()
    with page.expect_response(
        lambda response: response.url.endswith("/notes-runs") and response.request.method == "POST",
        timeout=5000,
    ) as accepted:
        button.click()
    assert accepted.value.status == 202
    assert bodies[0]["request_id"] == stored["request_id"]
    assert bodies[0]["provider"] == "chatgpt"
    assert bodies[0]["expected_generation_id"] == generation_id
    assert bodies[0]["expected_model"] == CHATGPT_MODEL
    assert bodies[0]["expected_reasoning_effort"] == "high"


def test_failed_reload_after_a_good_load_disables_start_despite_known_model(live, signed_in):
    _context, page = signed_in()
    meeting_id, _generation, _ = seed_completed_transcript(live.settings.data_dir)
    save_preference(page, live, "chatgpt", CHATGPT_MODEL, "high")
    mock_providers(page, live)
    page.goto(f"{live.url}/meetings/{meeting_id}#notes")
    expect(page.locator("#notes-start")).to_be_enabled()
    page.route(
        f"{live.url}/api/notes-preferences",
        lambda route: route.fulfill(status=500, json={"detail": {"code": "x", "message": "x"}}),
    )
    announce_choice_change(page)
    expect(page.locator("#notes-choice-reload")).to_be_visible()
    expect(page.locator("#notes-start")).to_be_disabled()
