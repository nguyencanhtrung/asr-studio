from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from fake_chatgpt import FakeChatGPTAdapter

from asr_studio.chatgpt import (
    DIRECT_SCOPE,
    DYNAMIC_CLIENT_ID,
    NOTES_HTTP_READ_TIMEOUT_SECONDS,
    NOTES_HTTP_TOTAL_TIMEOUT_SECONDS,
    REQUESTED_SCOPES,
    ChatGPTError,
    ChatGPTHTTPAdapter,
    ChatGPTProvider,
    ProviderStore,
)
from asr_studio.config import Settings

CALLBACK_URL = "http://127.0.0.1:8765/auth/chatgpt/callback"
ISSUED_CLIENT_ID = "oaiapp_test-123456"


class MutableClock:
    def __init__(self, value: float = 1_800_000_000) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def test_settings_keep_old_positional_order_and_isolate_default_provider_directory(tmp_path):
    data_dir = tmp_path / "install-a" / "data"
    auth_dir = tmp_path / "install-a" / "auth"
    first = Settings(data_dir, auth_dir, "http://localhost:8765")
    second = Settings(tmp_path / "install-b" / "data", tmp_path / "install-b" / "auth")

    assert first.public_url == "http://localhost:8765"
    assert first.provider_dir == tmp_path / "install-a" / "provider"
    assert second.provider_dir == tmp_path / "install-b" / "provider"
    assert first.chatgpt_callback_url == CALLBACK_URL


def test_https_public_url_requires_explicitly_supported_http_callback_mode(tmp_path):
    settings = Settings(tmp_path / "data", tmp_path / "auth", "https://127.0.0.1:8765")
    assert settings.chatgpt_callback_url is None
    configured = Settings(
        tmp_path / "data",
        tmp_path / "auth",
        "https://127.0.0.1:8765",
        chatgpt_callback_url=CALLBACK_URL,
    )
    assert configured.chatgpt_callback_url == CALLBACK_URL


def provider_for(tmp_path, adapter=None, *, clock=None, sleeper=time.sleep):
    provider = ChatGPTProvider(
        tmp_path / "provider",
        callback_url=CALLBACK_URL,
        adapter=adapter or FakeChatGPTAdapter(),
        clock=clock or MutableClock(),
        sleeper=sleeper,
    )
    provider.ensure()
    return provider


def callback_pairs(started, *, returned_client_id=ISSUED_CLIENT_ID, code="one-time-code"):
    query = parse_qs(urlsplit(started["authorization_url"]).query)
    pairs = [("state", query["state"][0]), ("code", code)]
    if returned_client_id is not None:
        pairs.append(("client_id", returned_client_id))
    return pairs


def callback_url(started, *, returned_client_id=ISSUED_CLIENT_ID, code="one-time-code"):
    pairs = callback_pairs(started, returned_client_id=returned_client_id, code=code)
    return f"{CALLBACK_URL}?{urlencode(pairs)}"


def connect(provider, *, returned_client_id=ISSUED_CLIENT_ID):
    started = provider.start_login()
    provider.handle_callback(callback_pairs(started, returned_client_id=returned_client_id))
    return started


def test_authorization_uses_dynamic_registration_pkce_scopes_and_stable_host(tmp_path):
    adapter = FakeChatGPTAdapter()
    provider = provider_for(tmp_path, adapter)

    started = provider.start_login()
    query = parse_qs(urlsplit(started["authorization_url"]).query)
    assert query["client_id"] == [DYNAMIC_CLIENT_ID]
    assert query["agent_name_hint"] == ["ASR Studio"]
    assert query["redirect_uri"] == [CALLBACK_URL]
    assert query["scope"] == [REQUESTED_SCOPES]
    assert query["code_challenge_method"] == ["S256"]
    expected_challenge = (
        base64.urlsafe_b64encode(
            hashlib.sha256(
                provider.store._read()["sessions"][started["id"]]["verifier"].encode("ascii")
            ).digest()
        )
        .rstrip(b"=")
        .decode("ascii")
    )
    assert query["code_challenge"] == [expected_challenge]
    assert "id_token_hint" not in query
    assert "login_hint" not in query
    assert "verifier" not in started and "nonce" not in started and "state" not in started

    host_id = provider.store._read()["host_id"]
    provider.handle_callback(callback_pairs(started))
    connected = provider.get_providers()["providers"][0]
    assert connected["status"] == "connected"
    assert connected["account"] == {"label": "verified@example.test"}
    assert connected["connection_version"] == 1
    assert adapter.exchange_calls[0]["client_id"] == ISSUED_CLIENT_ID
    assert adapter.exchange_calls[0]["redirect_uri"] == CALLBACK_URL
    assert adapter.exchange_calls[0]["verifier"] not in started["authorization_url"]
    assert "access-secret-for-tests" not in json.dumps(provider.get_providers())
    assert "refresh-secret-for-tests" not in json.dumps(provider.login_session(started["id"]))

    disconnected = provider.disconnect()
    assert disconnected == {"status": "disconnected", "remote_revocation_confirmed": True}
    state = provider.store._read()
    assert state["host_id"] == host_id
    assert state["active_client_id"] is None
    assert state["provisional_client_id"] == ISSUED_CLIENT_ID
    assert state["registrations"] == {}

    returning = provider.start_login()
    returning_query = parse_qs(urlsplit(returning["authorization_url"]).query)
    assert returning_query["client_id"] == [ISSUED_CLIENT_ID]
    assert returning_query["ext_agent_host_id"] == [host_id]
    assert "login_hint" not in returning_query
    assert "agent_name_hint" not in returning_query
    assert "id_token_hint" not in returning_query
    provider.handle_callback(callback_pairs(returning, returned_client_id=None, code="second-code"))
    assert provider.get_providers()["providers"][0]["status"] == "connected"


def test_pasted_callback_is_exact_loopback_json_equivalent_and_session_bound(tmp_path):
    adapter = FakeChatGPTAdapter()
    provider = provider_for(tmp_path, adapter)
    started = provider.start_login()

    wrong_session = "A" * 20
    with pytest.raises(ChatGPTError) as cross_session:
        provider.submit_callback(wrong_session, callback_url(started))
    assert cross_session.value.code == "invalid_or_replayed_state"
    assert provider.login_session(started["id"])["status"] == "pending"
    assert adapter.exchange_calls == []

    invalid_urls = [
        callback_url(started).replace(CALLBACK_URL, "https://127.0.0.1:8765/auth/chatgpt/callback"),
        callback_url(started).replace(CALLBACK_URL, f"{CALLBACK_URL}/extra"),
        callback_url(started) + "#fragment",
        callback_url(started).replace("http://127.0.0.1", "http://user:pass@127.0.0.1"),
        callback_url(started) + "&state=duplicate",
        callback_url(started) + "&unknown=" + ("x" * 20_000),
        callback_url(started)[:-1] + "\n",
    ]
    for pasted in invalid_urls:
        with pytest.raises(ChatGPTError) as invalid:
            provider.submit_callback(started["id"], pasted)
        assert invalid.value.code == "invalid_callback_url"
    assert provider.login_session(started["id"])["status"] == "pending"
    assert adapter.exchange_calls == []

    completed = provider.submit_callback(started["id"], callback_url(started))
    assert completed["status"] == "connected"
    assert adapter.exchange_calls[0]["code"] == "one-time-code"
    assert callback_url(started) not in json.dumps(provider.store._read())
    with pytest.raises(ChatGPTError) as replayed:
        provider.submit_callback(started["id"], callback_url(started))
    assert replayed.value.code == "invalid_or_replayed_state"


@pytest.mark.parametrize("encoded_code", ["bad%ZZ", "bad%", "%FF"])
def test_pasted_callback_rejects_broken_encoding_before_consuming_session(tmp_path, encoded_code):
    adapter = FakeChatGPTAdapter()
    provider = provider_for(tmp_path, adapter)
    started = provider.start_login()
    query = urlencode([pair for pair in callback_pairs(started) if pair[0] != "code"])
    with pytest.raises(ChatGPTError) as invalid:
        provider.submit_callback(started["id"], f"{CALLBACK_URL}?{query}&code={encoded_code}")
    assert invalid.value.code == "invalid_callback_url"
    assert adapter.exchange_calls == []
    assert provider.login_session(started["id"])["status"] == "pending"
    assert provider.submit_callback(started["id"], callback_url(started))["status"] == "connected"


def test_callback_rejects_non_ascii_state_without_comparison_error(tmp_path):
    provider = provider_for(tmp_path)
    started = provider.start_login()
    with pytest.raises(ChatGPTError) as invalid:
        provider.submit_callback(
            started["id"], f"{CALLBACK_URL}?{urlencode({'state': 'é' * 32, 'code': 'fake'})}"
        )
    assert invalid.value.code == "invalid_callback"
    assert provider.login_session(started["id"])["status"] == "pending"


def test_registration_recreation_invalidates_saved_connection_version_and_choice(tmp_path):
    provider = provider_for(tmp_path)
    connect(provider)
    with provider.store.locked(write=True) as state:
        state["notes_provider"] = "chatgpt"
    before = provider.store._read()
    provider.recreate_registration()
    after = provider.store._read()
    assert after["connection_version"] == before["connection_version"] + 1
    assert after["notes_provider"] is None
    assert after["host_id"] == before["host_id"]


def test_registration_recreation_is_explicit_and_normal_login_reuses_registration(tmp_path):
    adapter = FakeChatGPTAdapter()
    provider = provider_for(tmp_path, adapter)
    connect(provider)
    host_id = provider.store._read()["host_id"]

    recreated = provider.recreate_registration(display_name="My ASR Studio")
    query = parse_qs(urlsplit(recreated["authorization_url"]).query)
    assert recreated["registration_recreated"] is True
    assert query["client_id"] == [DYNAMIC_CLIENT_ID]
    assert query["agent_name_hint"] == ["My ASR Studio"]
    state = provider.store._read()
    assert state["host_id"] == host_id
    assert state["connection_status"] == "disconnected"
    assert state["active_client_id"] is None
    assert state["registrations"] == {}

    provider.handle_callback(callback_pairs(recreated))
    returning = provider.start_login()
    returning_query = parse_qs(urlsplit(returning["authorization_url"]).query)
    assert returning_query["client_id"] == [ISSUED_CLIENT_ID]
    assert "agent_name_hint" not in returning_query


def test_registration_recreation_keeps_old_registration_when_revoke_is_unconfirmed(tmp_path):
    adapter = FakeChatGPTAdapter()
    provider = provider_for(tmp_path, adapter)
    connect(provider)
    before = provider.store._read()
    adapter.revoke_results.clear()
    adapter.revoke_results.append(False)

    with pytest.raises(ChatGPTError) as unconfirmed:
        provider.recreate_registration(display_name="New name")

    assert unconfirmed.value.code == "revocation_unconfirmed"
    after = provider.store._read()
    assert after == before
    assert provider.get_providers()["providers"][0]["status"] == "connected"
    assert len(after["sessions"]) == len(before["sessions"])


def test_new_provider_instance_replaces_pending_login_and_rejects_old_callback(tmp_path):
    adapter = FakeChatGPTAdapter()
    clock = MutableClock()
    provider = provider_for(tmp_path, adapter, clock=clock)
    old_session = provider.start_login()
    old_query = parse_qs(urlsplit(old_session["authorization_url"]).query)

    other_caller = ChatGPTProvider(
        provider.store.directory,
        callback_url=CALLBACK_URL,
        adapter=adapter,
        clock=clock,
    )
    other_caller.ensure()
    new_session = other_caller.start_login()
    new_query = parse_qs(urlsplit(new_session["authorization_url"]).query)

    assert "replaced_previous" not in old_session
    assert new_session["id"] != old_session["id"]
    assert new_session["replaced_previous"] is True
    assert new_query["state"] != old_query["state"]
    assert new_query["nonce"] != old_query["nonce"]
    assert new_query["code_challenge"] != old_query["code_challenge"]
    old_state = provider.store._read()["sessions"][old_session["id"]]
    assert old_state["status"] == "cancelled"
    assert not {"state", "nonce", "verifier"} & old_state.keys()

    with pytest.raises(ChatGPTError) as late_callback:
        provider.handle_callback(callback_pairs(old_session))
    assert late_callback.value.code == "invalid_or_replayed_state"
    assert adapter.exchange_calls == []
    assert provider.login_session(new_session["id"])["status"] == "pending"

    provider.handle_callback(callback_pairs(new_session, code="replacement-code"))
    assert provider.login_session(old_session["id"])["status"] == "cancelled"
    assert provider.login_session(new_session["id"])["status"] == "connected"
    assert [call["code"] for call in adapter.exchange_calls] == ["replacement-code"]
    assert provider.get_providers()["providers"][0]["status"] == "connected"


def test_login_replacement_preserves_established_connection_and_preferences(tmp_path):
    adapter = FakeChatGPTAdapter()
    clock = MutableClock()
    provider = provider_for(tmp_path, adapter, clock=clock)
    connect(provider)
    provider.save_notes_preference(model="gpt-6.1-sol")
    provider.verify(model="gpt-6.1-sol")
    before = provider.store._read()

    old_session = provider.start_login()
    other_caller = ChatGPTProvider(
        provider.store.directory,
        callback_url=CALLBACK_URL,
        adapter=adapter,
        clock=clock,
    )
    other_caller.ensure()
    new_session = other_caller.start_login()
    after = provider.store._read()

    for key in (
        "connection_status",
        "active_client_id",
        "registrations",
        "notes_provider",
        "selected_model",
        "selected_reasoning_effort",
        "verification",
    ):
        assert after[key] == before[key]
    assert provider.get_providers()["providers"][0]["account"] == {"label": "verified@example.test"}
    assert provider.saved_notes_settings() == ("gpt-6.1-sol", "default")
    assert provider.login_session(old_session["id"])["status"] == "cancelled"
    assert provider.login_session(new_session["id"])["status"] == "pending"


def test_provider_state_is_private_and_atomic(tmp_path):
    provider = provider_for(tmp_path)
    state = provider.store._read()

    assert os.stat(provider.store.directory).st_mode & 0o777 == 0o700
    assert os.stat(provider.store.state_path).st_mode & 0o777 == 0o600
    assert state["host_id"].startswith("urn:uuid:")
    assert ProviderStore(provider.store.directory).state_path == provider.store.state_path


def test_cancel_and_replay_cannot_exchange_or_commit_callback(tmp_path):
    adapter = FakeChatGPTAdapter()
    provider = provider_for(tmp_path, adapter)
    cancelled = provider.start_login()

    assert provider.cancel_login(cancelled["id"])["status"] == "cancelled"
    with pytest.raises(ChatGPTError) as late_callback:
        provider.handle_callback(callback_pairs(cancelled))
    assert late_callback.value.code == "invalid_or_replayed_state"
    assert adapter.exchange_calls == []
    assert provider.get_providers()["providers"][0]["status"] == "disconnected"

    completed = connect(provider)
    with pytest.raises(ChatGPTError):
        provider.handle_callback(callback_pairs(completed))
    assert len(adapter.exchange_calls) == 1
    assert provider.login_session(completed["id"])["status"] == "connected"


def test_cancel_during_code_exchange_cancels_late_login_commit(tmp_path):
    adapter = FakeChatGPTAdapter()
    provider = provider_for(tmp_path, adapter)
    started = provider.start_login()
    callback = callback_pairs(started)
    adapter.exchange_release.clear()

    with ThreadPoolExecutor(max_workers=2) as executor:
        late_login = executor.submit(provider.handle_callback, callback)
        assert adapter.exchange_started.wait(timeout=2)
        cancelled_session = provider.cancel_login(started["id"])
        assert cancelled_session["status"] == "cancelled"
        adapter.exchange_release.set()
        with pytest.raises(ChatGPTError) as cancelled:
            late_login.result(timeout=2)

    assert adapter.revoke_calls == [
        {"client_id": ISSUED_CLIENT_ID, "refresh_token": "refresh-secret-for-tests"}
    ]
    assert cancelled.value.code == "login_cancelled"
    assert provider.get_providers()["providers"][0]["status"] == "disconnected"
    assert "access_token" not in provider.store._read()["registrations"].get(ISSUED_CLIENT_ID, {})


def test_start_login_remains_busy_while_callback_holds_operation_lock(tmp_path, monkeypatch):
    adapter = FakeChatGPTAdapter()
    provider = provider_for(tmp_path, adapter)
    other_caller = ChatGPTProvider(
        provider.store.directory,
        callback_url=CALLBACK_URL,
        adapter=adapter,
    )
    other_caller.ensure()
    started = provider.start_login()
    adapter.exchange_release.clear()
    monkeypatch.setattr("asr_studio.chatgpt.OPERATION_LOCK_TIMEOUT_SECONDS", 0.01)

    with ThreadPoolExecutor(max_workers=1) as executor:
        callback = executor.submit(provider.handle_callback, callback_pairs(started))
        assert adapter.exchange_started.wait(timeout=2)
        try:
            with pytest.raises(ChatGPTError) as busy:
                other_caller.start_login()
            assert busy.value.code == "provider_busy"
            assert provider.login_session(started["id"])["status"] == "pending"
        finally:
            adapter.exchange_release.set()
        callback.result(timeout=2)

    assert provider.login_session(started["id"])["status"] == "connected"


def test_expired_login_cannot_be_replayed_or_exchanged(tmp_path):
    clock = MutableClock()
    adapter = FakeChatGPTAdapter()
    provider = provider_for(tmp_path, adapter, clock=clock)
    started = provider.start_login()
    clock.value += 11 * 60

    assert provider.login_session(started["id"])["status"] == "expired"
    with pytest.raises(ChatGPTError) as expired:
        provider.handle_callback(callback_pairs(started))
    assert expired.value.code == "invalid_or_replayed_state"
    assert adapter.exchange_calls == []


def test_connected_reauthentication_rejects_client_id_and_subject_mismatch(tmp_path):
    adapter = FakeChatGPTAdapter()
    provider = provider_for(tmp_path, adapter)
    connect(provider)

    started = provider.start_login()
    with pytest.raises(ChatGPTError) as wrong_client:
        provider.handle_callback(callback_pairs(started, returned_client_id="oaiapp_other-12345"))
    assert wrong_client.value.code == "invalid_callback"
    assert provider.get_providers()["providers"][0]["status"] == "connected"

    started = provider.start_login()
    adapter.subject = "different-subject"
    with pytest.raises(ChatGPTError) as wrong_subject:
        provider.handle_callback(callback_pairs(started, returned_client_id=None))
    assert wrong_subject.value.code == "account_identity_mismatch"
    assert provider.get_providers()["providers"][0]["status"] == "connected"
    assert provider.store._read()["registrations"][ISSUED_CLIENT_ID]["subject"] != (
        "different-subject"
    )


def test_disconnect_allows_signing_in_with_a_different_account(tmp_path):
    adapter = FakeChatGPTAdapter()
    provider = provider_for(tmp_path, adapter)
    connected_session = connect(provider)
    with provider.store.locked(write=True) as state:
        # Simulate a closed session persisted by a version that did not scrub
        # the account binding when the login completed.
        state["sessions"][connected_session["id"]]["expected_subject"] = adapter.subject
    pending_reauthentication = provider.start_login()
    provider.disconnect()

    sessions = provider.store._read()["sessions"]
    cancelled = sessions[pending_reauthentication["id"]]
    assert cancelled["status"] == "cancelled"
    assert all("expected_subject" not in session for session in sessions.values())

    adapter.subject = "different-subject"
    adapter.email = "different@example.test"
    returning = provider.start_login()
    query = parse_qs(urlsplit(returning["authorization_url"]).query)
    assert query["client_id"] == [ISSUED_CLIENT_ID]
    assert "login_hint" not in query

    provider.handle_callback(callback_pairs(returning, returned_client_id=None))

    connected = provider.get_providers()["providers"][0]
    assert connected["status"] == "connected"
    assert connected["account"] == {"label": "different@example.test"}
    registration = provider.store._read()["registrations"][ISSUED_CLIENT_ID]
    assert registration["subject"] == "different-subject"


def test_startup_migrates_legacy_disconnected_account_binding(tmp_path):
    adapter = FakeChatGPTAdapter()
    provider = provider_for(tmp_path, adapter)
    connected_session = connect(provider)

    with provider.store.locked(write=True) as state:
        registration = state["registrations"][ISSUED_CLIENT_ID]
        for key in ("access_token", "refresh_token", "id_token", "expires_at"):
            registration.pop(key, None)
        state["connection_status"] = "disconnected"
        state["sessions"][connected_session["id"]]["expected_subject"] = adapter.subject

    legacy_pending = provider.start_login()
    legacy_query = parse_qs(urlsplit(legacy_pending["authorization_url"]).query)
    assert legacy_query["login_hint"] == [adapter.email]

    restarted = ChatGPTProvider(
        provider.store.directory,
        callback_url=CALLBACK_URL,
        adapter=adapter,
        clock=MutableClock(),
    )
    restarted.ensure()

    migrated = restarted.store._read()
    assert migrated["active_client_id"] is None
    assert migrated["provisional_client_id"] == ISSUED_CLIENT_ID
    assert migrated["registrations"] == {}
    assert migrated["sessions"][legacy_pending["id"]]["status"] == "pending"
    assert all("expected_subject" not in session for session in migrated["sessions"].values())

    adapter.subject = "different-subject"
    adapter.email = "different@example.test"
    returning = restarted.start_login()
    query = parse_qs(urlsplit(returning["authorization_url"]).query)
    assert query["client_id"] == [ISSUED_CLIENT_ID]
    assert "login_hint" not in query
    restarted.handle_callback(callback_pairs(returning, returned_client_id=None))
    assert restarted.get_providers()["providers"][0]["account"] == {
        "label": "different@example.test"
    }


def test_missing_direct_scope_requires_reconnect_without_inference(tmp_path):
    adapter = FakeChatGPTAdapter()
    adapter.scopes = "openid profile offline_access"
    provider = provider_for(tmp_path, adapter)
    connect(provider)

    assert provider.get_providers()["providers"][0]["status"] == "reconnect_required"
    with pytest.raises(ChatGPTError) as no_scope:
        provider.list_models()
    assert no_scope.value.code == "reconnect_required"
    assert adapter.model_calls == []
    assert DIRECT_SCOPE not in provider.store._read()["registrations"][ISSUED_CLIENT_ID]["scopes"]


def test_refresh_rotation_is_serialized_across_provider_instances(tmp_path):
    clock = MutableClock()
    adapter = FakeChatGPTAdapter()
    provider = provider_for(tmp_path, adapter, clock=clock)
    connect(provider)
    with provider.store.locked(write=True) as state:
        state["registrations"][ISSUED_CLIENT_ID]["expires_at"] = clock.value - 1

    second_process = ChatGPTProvider(
        provider.store.directory,
        callback_url=CALLBACK_URL,
        adapter=adapter,
        clock=clock,
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda item: item.list_models(), [provider, second_process]))

    assert [model["id"] for model in results[0]["models"]] == [
        "gpt-6.1-sol",
        "gpt-6-luna",
        "gpt-5.6-luna",
    ]
    assert [model["id"] for model in results[1]["models"]] == [
        "gpt-6.1-sol",
        "gpt-6-luna",
        "gpt-5.6-luna",
    ]
    assert len(adapter.refresh_calls) == 1
    assert adapter.refresh_calls[0]["refresh_token"] == "refresh-secret-for-tests"
    saved = provider.store._read()["registrations"][ISSUED_CLIENT_ID]
    assert saved["access_token"] == "rotated-access-secret"
    assert saved["refresh_token"] == "rotated-refresh-secret"


@pytest.mark.parametrize(
    "error,expected_status",
    [
        (ChatGPTError(503, "provider_unavailable", "unavailable"), "connected"),
        (ChatGPTError(401, "invalid_grant", "expired"), "reconnect_required"),
    ],
)
def test_refresh_failure_preserves_transient_tokens_and_clears_terminal_tokens(
    tmp_path, error, expected_status
):
    clock = MutableClock()
    adapter = FakeChatGPTAdapter()
    provider = provider_for(tmp_path, adapter, clock=clock)
    connect(provider)
    with provider.store.locked(write=True) as state:
        state["registrations"][ISSUED_CLIENT_ID]["expires_at"] = clock.value - 1
    adapter.refresh_results.append(error)

    with pytest.raises(ChatGPTError) as response_error:
        provider.list_models()
    assert response_error.value.status_code != 401
    assert provider.get_providers()["providers"][0]["status"] == expected_status
    registration = provider.store._read()["registrations"][ISSUED_CLIENT_ID]
    if expected_status == "connected":
        assert registration["access_token"] == "access-secret-for-tests"
        assert registration["refresh_token"] == "refresh-secret-for-tests"
    else:
        assert "access_token" not in registration
        assert "refresh_token" not in registration


def test_scope_down_refresh_requires_reconnect(tmp_path):
    clock = MutableClock()
    adapter = FakeChatGPTAdapter()
    provider = provider_for(tmp_path, adapter, clock=clock, sleeper=lambda _: None)
    connect(provider)
    with provider.store.locked(write=True) as state:
        state["registrations"][ISSUED_CLIENT_ID]["expires_at"] = clock.value - 1
    adapter.refresh_results.append(
        {
            "access_token": "scope-down-access",
            "refresh_token": "scope-down-refresh",
            "token_type": "Bearer",
            "expires_in": 3600,
            "scope": "openid offline_access",
        }
    )
    with pytest.raises(ChatGPTError) as scope_down:
        provider.list_models()
    assert scope_down.value.code == "reconnect_required"
    assert provider.get_providers()["providers"][0]["status"] == "reconnect_required"
    assert provider.store._read()["registrations"][ISSUED_CLIENT_ID]["refresh_token"] == (
        "scope-down-refresh"
    )
    provider.disconnect()
    assert adapter.revoke_calls[-1]["refresh_token"] == "scope-down-refresh"

    state = provider.store._read()
    assert state["registrations"] == {}
    assert state["provisional_client_id"] == ISSUED_CLIENT_ID


def test_disconnect_clears_local_tokens_after_bounded_revocation_failures(tmp_path):
    adapter = FakeChatGPTAdapter()
    provider = provider_for(tmp_path, adapter, sleeper=lambda _: None)
    connect(provider)
    adapter.revoke_results.clear()
    adapter.revoke_results.extend(
        [
            ChatGPTError(503, "revocation_unavailable", "not confirmed"),
            ChatGPTError(502, "revocation_unavailable", "not confirmed"),
            False,
        ]
    )

    result = provider.disconnect()

    assert result == {"status": "disconnected", "remote_revocation_confirmed": False}
    assert len(adapter.revoke_calls) == 3
    state = provider.store._read()
    assert state["registrations"] == {}
    assert state["provisional_client_id"] == ISSUED_CLIENT_ID


def test_verification_and_disconnect_serialize_without_provider_locking_other_work(tmp_path):
    adapter = FakeChatGPTAdapter()
    provider = provider_for(tmp_path, adapter)
    connect(provider)
    adapter.verification_release.clear()

    with ThreadPoolExecutor(max_workers=2) as executor:
        verification = executor.submit(provider.verify, model="gpt-6.1-sol")
        assert adapter.verification_started.wait(timeout=2)
        disconnection = executor.submit(provider.disconnect)
        time.sleep(0.05)
        assert not disconnection.done()
        # This is the same stateless path used by the app while provider work is locked.
        assert provider.saved_notes_settings() == (None, "default")
        adapter.verification_release.set()
        assert verification.result(timeout=2) == {
            "model": "gpt-6.1-sol",
            "reasoning_effort": "default",
            "verified_at": "2027-01-15T08:00:00Z",
            "output": {"ok": True},
        }
        assert disconnection.result(timeout=2)["status"] == "disconnected"
    assert len(adapter.verify_calls) == 1
    assert adapter.verify_calls[0]["model"] == "gpt-6.1-sol"
    assert provider.get_providers()["providers"][0]["verification"] is None


def test_disconnect_returns_retryable_busy_without_clearing_tokens_during_operation(
    tmp_path, monkeypatch
):
    import asr_studio.chatgpt as chatgpt

    monkeypatch.setattr(chatgpt, "OPERATION_LOCK_TIMEOUT_SECONDS", 0.05, raising=False)
    adapter = FakeChatGPTAdapter()
    provider = provider_for(tmp_path, adapter)
    connect(provider)
    operation_held = threading.Event()
    release_operation = threading.Event()

    def hold_operation():
        with provider.store.operation():
            operation_held.set()
            assert release_operation.wait(timeout=2)

    with ThreadPoolExecutor(max_workers=2) as executor:
        holder = executor.submit(hold_operation)
        assert operation_held.wait(timeout=2)
        disconnect_started = time.monotonic()
        disconnecting = executor.submit(provider.disconnect)
        try:
            with pytest.raises(ChatGPTError) as busy:
                disconnecting.result(timeout=0.5)
            elapsed = time.monotonic() - disconnect_started
        finally:
            release_operation.set()
            holder.result(timeout=2)

    assert busy.value.code == "provider_busy"
    assert busy.value.status_code == 503
    assert elapsed < 0.3
    assert adapter.revoke_calls == []
    state = provider.store._read()
    registration = state["registrations"][ISSUED_CLIENT_ID]
    assert state["connection_status"] == "connected"
    assert registration["access_token"] == "access-secret-for-tests"
    assert registration["refresh_token"] == "refresh-secret-for-tests"


def test_invalid_authorization_context_does_not_force_consent(tmp_path):
    adapter = FakeChatGPTAdapter()
    provider = provider_for(tmp_path, adapter)
    connect(provider)
    adapter.models_error = ChatGPTError(
        403, "chatpass_v2_invalid_authorization_context", "private provider detail"
    )

    with pytest.raises(ChatGPTError):
        provider.list_models()

    recovery = provider.start_login()
    query = parse_qs(urlsplit(recovery["authorization_url"]).query)
    assert "prompt" not in query


def test_scope_recovery_requests_consent_only_when_permission_is_missing(tmp_path):
    adapter = FakeChatGPTAdapter()
    adapter.scopes = "openid profile email offline_access resource.invoke"
    provider = provider_for(tmp_path, adapter)
    connect(provider)
    assert provider.get_providers()["providers"][0]["status"] == "reconnect_required"
    recovery = provider.start_login()
    assert parse_qs(urlsplit(recovery["authorization_url"]).query)["prompt"] == ["consent"]
    adapter.scopes += " chatgpt.tokens.use.direct"
    provider.handle_callback(callback_pairs(recovery, returned_client_id=None))
    ordinary = provider.start_login()
    assert "prompt" not in parse_qs(urlsplit(ordinary["authorization_url"]).query)


def test_missing_callback_does_not_disable_local_app_but_blocks_provider_login(tmp_path):
    provider = ChatGPTProvider(tmp_path / "provider", callback_url="", adapter=FakeChatGPTAdapter())
    provider.ensure()
    assert provider.get_providers()["providers"][0]["status"] == "disconnected"
    with pytest.raises(ChatGPTError) as failure:
        provider.start_login()
    assert failure.value.code == "callback_not_configured"


def test_notes_http_client_bounds_silent_reads_but_keeps_long_task_budget():
    import httpx

    adapter = ChatGPTHTTPAdapter(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200))
    )
    with adapter._client(read_timeout=NOTES_HTTP_READ_TIMEOUT_SECONDS) as client:
        assert client.timeout.read == NOTES_HTTP_READ_TIMEOUT_SECONDS
    assert NOTES_HTTP_READ_TIMEOUT_SECONDS < NOTES_HTTP_TOTAL_TIMEOUT_SECONDS
    assert NOTES_HTTP_TOTAL_TIMEOUT_SECONDS == 900.0


@pytest.mark.parametrize(
    "body",
    [
        {"detail": "sensitive provider detail"},
        {"error": {"code": "invalid_api_key", "message": "sensitive token"}},
    ],
)
def test_real_http_401_marks_reconnect_and_preserves_only_safe_diagnostics(tmp_path, body):
    import httpx

    from asr_studio.chatgpt import ChatGPTHTTPAdapter

    provider = provider_for(tmp_path)
    connect(provider)
    provider.adapter = ChatGPTHTTPAdapter(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(401, json=body, headers={"x-request-id": "req_unit123"})
        )
    )
    with pytest.raises(ChatGPTError) as failure:
        provider.list_models()
    assert failure.value.status_code == 409
    assert failure.value.code == "provider_auth_error"
    assert provider.get_providers()["providers"][0]["status"] == "reconnect_required"
    state = provider.store._read()
    assert state["last_diagnostic"]["request_id"] == "req_unit123"
    assert state["last_diagnostic"]["http_status"] == 401
    assert "sensitive" not in json.dumps(state)


def test_busy_callback_does_not_claim_state_or_exchange_code(tmp_path, monkeypatch):
    import asr_studio.chatgpt as chatgpt

    monkeypatch.setattr(chatgpt, "OPERATION_LOCK_TIMEOUT_SECONDS", 0.01)
    adapter = FakeChatGPTAdapter()
    provider = provider_for(tmp_path, adapter)
    started = provider.start_login()
    with provider.store.operation(), pytest.raises(ChatGPTError) as busy:
        provider.handle_callback(callback_pairs(started))
    assert busy.value.code == "provider_busy"
    assert not adapter.exchange_calls
    assert not provider.store._read()["sessions"][started["id"]].get("claimed")
    provider.handle_callback(callback_pairs(started))
    assert provider.login_session(started["id"])["status"] == "connected"


def test_unexpected_revoke_failure_still_clears_local_credentials(tmp_path, monkeypatch):
    adapter = FakeChatGPTAdapter()
    provider = provider_for(tmp_path, adapter)
    connect(provider)

    def fail_revoke(**kwargs):
        raise ValueError("malformed provider document")

    monkeypatch.setattr(adapter, "revoke", fail_revoke)
    assert provider.disconnect() == {"status": "disconnected", "remote_revocation_confirmed": False}
    state = provider.store._read()
    assert state["registrations"] == {}
    assert state["provisional_client_id"] == ISSUED_CLIENT_ID


def test_initial_invalid_grant_retains_client_for_unverified_retry_after_restart(tmp_path):
    adapter = FakeChatGPTAdapter()
    provider = provider_for(tmp_path, adapter)
    started = provider.start_login()
    adapter.exchange_error = ChatGPTError(400, "invalid_grant", "expired code")
    with pytest.raises(ChatGPTError):
        provider.handle_callback(callback_pairs(started))
    state = provider.store._read()
    assert state["provisional_client_id"] == ISSUED_CLIENT_ID
    assert state["active_client_id"] is None
    assert state["registrations"] == {}
    assert provider.login_session(started["id"])["status"] == "failed"
    assert provider.get_providers()["providers"][0]["status"] == "disconnected"

    provider = provider_for(tmp_path, adapter)
    retry = provider.start_login()
    query = parse_qs(urlsplit(retry["authorization_url"]).query)
    assert query["client_id"] == [ISSUED_CLIENT_ID]
    assert "agent_name_hint" not in query
    assert "login_hint" not in query
    adapter.exchange_error = None
    provider.handle_callback(callback_pairs(retry, returned_client_id=None))
    state = provider.store._read()
    assert "provisional_client_id" not in state
    assert state["active_client_id"] == ISSUED_CLIENT_ID
    assert state["registrations"][ISSUED_CLIENT_ID]["subject"] == adapter.subject


@pytest.mark.parametrize("code", ["provider_auth_error", "subscription_sharing_invalid_user"])
def test_reconnect_blocks_use_but_retains_refresh_for_disconnect(tmp_path, code):
    adapter = FakeChatGPTAdapter()
    provider = provider_for(tmp_path, adapter)
    connect(provider)
    adapter.models_error = ChatGPTError(401, code, "invalid credential")
    with pytest.raises(ChatGPTError):
        provider.list_models()
    registration = provider.store._read()["registrations"][ISSUED_CLIENT_ID]
    assert registration["refresh_token"] == "refresh-secret-for-tests"
    assert "access_token" not in registration
    calls = len(adapter.model_calls)
    with pytest.raises(ChatGPTError) as blocked:
        provider.list_models()
    assert blocked.value.code == "reconnect_required"
    assert len(adapter.model_calls) == calls
    assert provider.disconnect()["remote_revocation_confirmed"] is True
    assert adapter.revoke_calls[-1]["refresh_token"] == "refresh-secret-for-tests"
    assert provider.store._read()["registrations"] == {}


def test_invalid_subscriber_requires_confirmed_revoke_before_new_authorization(tmp_path):
    adapter = FakeChatGPTAdapter()
    provider = provider_for(tmp_path, adapter)
    connect(provider)
    adapter.models_error = ChatGPTError(401, "subscription_sharing_invalid_user", "invalid user")
    with pytest.raises(ChatGPTError):
        provider.list_models()
    adapter.revoke_results.clear()
    adapter.revoke_results.extend([False, True])
    before_sessions = len(provider.store._read()["sessions"])
    with pytest.raises(ChatGPTError) as unconfirmed:
        provider.start_login()
    assert unconfirmed.value.code == "revocation_unconfirmed"
    assert len(provider.store._read()["sessions"]) == before_sessions
    assert provider.store._read()["registrations"][ISSUED_CLIENT_ID]["refresh_token"]
    retry = provider.start_login()
    assert retry["status"] == "pending"
    assert len(adapter.revoke_calls) == 2
    assert "refresh_token" not in provider.store._read()["registrations"][ISSUED_CLIENT_ID]


@pytest.mark.parametrize(
    "code",
    [
        "provider_auth_error",
        "subscription_sharing_invalid_user",
        "chatpass_v2_scope_not_authorized",
        "chatpass_v2_invalid_authorization_context",
    ],
)
def test_refresh_auth_failure_blocks_retries_and_preserves_revocation_token(tmp_path, code):
    clock = MutableClock()
    adapter = FakeChatGPTAdapter()
    provider = provider_for(tmp_path, adapter, clock=clock)
    connect(provider)
    with provider.store.locked(write=True) as state:
        state["registrations"][ISSUED_CLIENT_ID]["expires_at"] = clock.value - 1
    adapter.refresh_results.append(ChatGPTError(403, code, "private provider detail"))
    with pytest.raises(ChatGPTError):
        provider.list_models()
    assert provider.get_providers()["providers"][0]["status"] == "reconnect_required"
    registration = provider.store._read()["registrations"][ISSUED_CLIENT_ID]
    assert registration["refresh_token"] == "refresh-secret-for-tests"
    assert "access_token" not in registration and "id_token" not in registration
    with pytest.raises(ChatGPTError) as blocked:
        provider.list_models()
    assert blocked.value.code == "reconnect_required"
    assert len(adapter.refresh_calls) == 1
    assert provider.disconnect()["remote_revocation_confirmed"] is True


def test_rejected_identity_attempts_revoke_even_if_revocation_unavailable(tmp_path):
    adapter = FakeChatGPTAdapter()
    provider = provider_for(tmp_path, adapter)
    connect(provider)
    returning = provider.start_login()
    adapter.subject = "different-subject"
    adapter.revoke_results.clear()
    adapter.revoke_results.append(ChatGPTError(503, "provider_unavailable", "unavailable"))
    before = len(adapter.revoke_calls)
    with pytest.raises(ChatGPTError) as mismatch:
        provider.handle_callback(callback_pairs(returning, returned_client_id=None))
    assert mismatch.value.code == "account_identity_mismatch"
    assert len(adapter.revoke_calls) == before + 1
    assert provider.get_providers()["providers"][0]["status"] == "connected"
    assert provider.store._read()["registrations"][ISSUED_CLIENT_ID]["subject"] != (
        "different-subject"
    )
