from __future__ import annotations

import os

import pytest

from asr_studio.auth import (
    AuthStateError,
    AuthStore,
    create_session_cookie,
    session_is_valid,
    token_matches,
)


def test_auth_state_is_durable_private_and_unique_per_install(tmp_path):
    first = AuthStore(tmp_path / "install-one")
    second = AuthStore(tmp_path / "install-two")
    initial = first.ensure()
    reopened = AuthStore(tmp_path / "install-one").load()
    other_install = second.ensure()

    assert initial.token == reopened.token
    assert initial.signing_secret == reopened.signing_secret
    assert initial.token != other_install.token
    assert os.stat(first.directory).st_mode & 0o777 == 0o700
    assert os.stat(first.state_path).st_mode & 0o777 == 0o600


def test_token_comparison_and_cookie_have_no_time_expiry(tmp_path):
    store = AuthStore(tmp_path / "auth")
    state = store.ensure()
    cookie = create_session_cookie(state)

    assert token_matches(state.token, state.token)
    assert not token_matches(state.token, "wrong-token")
    assert session_is_valid(cookie, state)


def test_origin_bound_sessions_reject_replay_and_keep_legacy_on_primary_only(tmp_path):
    state = AuthStore(tmp_path / "auth").ensure()
    primary = "https://primary.example.test"
    alias = "https://alias.example.test"
    legacy = create_session_cookie(state)
    primary_cookie = create_session_cookie(state, origin=primary)
    alias_cookie = create_session_cookie(state, origin=alias)

    assert session_is_valid(legacy, state, primary, allow_legacy=True)
    assert not session_is_valid(legacy, state, alias, allow_legacy=False)
    assert session_is_valid(primary_cookie, state, primary, allow_legacy=True)
    assert not session_is_valid(primary_cookie, state, alias, allow_legacy=False)
    assert session_is_valid(alias_cookie, state, alias, allow_legacy=False)
    assert not session_is_valid(alias_cookie, state, primary, allow_legacy=True)

    rotated = AuthStore(tmp_path / "auth").rotate()
    assert not session_is_valid(primary_cookie, rotated, primary, allow_legacy=True)
    assert not session_is_valid(alias_cookie, rotated, alias, allow_legacy=False)


def test_malformed_cookie_is_rejected(tmp_path):
    state = AuthStore(tmp_path / "auth").ensure()

    for cookie in (None, "", "invalid", "a.b.c", "x" * 600):
        assert not session_is_valid(cookie, state)


def test_load_fails_closed_instead_of_replacing_missing_state(tmp_path):
    store = AuthStore(tmp_path / "auth")
    store.ensure()
    store.state_path.unlink()

    with pytest.raises(AuthStateError):
        store.load()

    assert not store.state_path.exists()
