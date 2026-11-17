"""Durable per-install token state and generation-bound signed sessions."""

from __future__ import annotations

import base64
import binascii
import fcntl
import hashlib
import hmac
import json
import os
import secrets
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path


class AuthStateError(RuntimeError):
    """Raised when the protected authentication state cannot be used safely."""


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


@dataclass(frozen=True, slots=True)
class AuthState:
    token: str = field(repr=False)
    signing_secret: str = field(repr=False)
    generation: int


class AuthStore:
    """Manage a single installation's token and cookie signing key atomically."""

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        self.state_path = self.directory / "auth.json"
        self.lock_path = self.directory / ".auth.lock"

    @contextmanager
    def _locked(self, *, exclusive: bool) -> Iterator[None]:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.directory, 0o700)
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            lock_fd = os.open(self.lock_path, flags, 0o600)
        except OSError as exc:
            raise AuthStateError("Cannot safely open authentication lock") from exc
        try:
            os.fchmod(lock_fd, 0o600)
            fcntl.flock(lock_fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            yield
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)

    @staticmethod
    def _new_state(generation: int = 1) -> AuthState:
        return AuthState(
            token=secrets.token_urlsafe(32),
            signing_secret=_b64url_encode(secrets.token_bytes(32)),
            generation=generation,
        )

    def _read_locked(self) -> AuthState:
        if self.state_path.is_symlink():
            raise AuthStateError("Authentication state must not be a symbolic link")
        try:
            raw = self.state_path.read_text(encoding="utf-8")
            document = json.loads(raw)
            state = AuthState(
                token=document["token"],
                signing_secret=document["signing_secret"],
                generation=document["generation"],
            )
            if (
                not isinstance(state.token, str)
                or len(state.token) < 40
                or not isinstance(state.signing_secret, str)
                or len(_b64url_decode(state.signing_secret)) != 32
                or not isinstance(state.generation, int)
                or state.generation < 1
            ):
                raise ValueError("invalid authentication fields")
            os.chmod(self.state_path, 0o600)
            return state
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise AuthStateError("Authentication state is missing or invalid") from exc

    def _write_locked(self, state: AuthState) -> None:
        document = json.dumps(
            {
                "version": 1,
                "token": state.token,
                "signing_secret": state.signing_secret,
                "generation": state.generation,
            },
            separators=(",", ":"),
        )
        descriptor, temporary_name = tempfile.mkstemp(prefix=".auth-", dir=self.directory)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as temporary_file:
                temporary_file.write(document)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.replace(temporary_name, self.state_path)
            os.chmod(self.state_path, 0o600)
            directory_fd = os.open(self.directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError as exc:
            with suppress(FileNotFoundError):
                os.unlink(temporary_name)
            raise AuthStateError("Cannot persist authentication state safely") from exc

    def ensure(self) -> AuthState:
        with self._locked(exclusive=True):
            if not self.state_path.exists():
                self._write_locked(self._new_state())
            return self._read_locked()

    def load(self) -> AuthState:
        with self._locked(exclusive=False):
            return self._read_locked()

    def rotate(self) -> AuthState:
        with self._locked(exclusive=True):
            current = self._new_state()
            if self.state_path.exists():
                previous = self._read_locked()
                current = AuthState(
                    token=secrets.token_urlsafe(32),
                    signing_secret=previous.signing_secret,
                    generation=previous.generation + 1,
                )
            self._write_locked(current)
            return current


def token_matches(candidate: str, expected: str) -> bool:
    """Compare fixed-size digests so token bytes are never compared by early exit."""
    candidate_digest = hashlib.sha256(candidate.encode("utf-8")).digest()
    expected_digest = hashlib.sha256(expected.encode("utf-8")).digest()
    return hmac.compare_digest(candidate_digest, expected_digest)


def create_session_cookie(state: AuthState, origin: str | None = None) -> str:
    """Create a legacy cookie or an origin-bound v2 cookie."""
    return _create_session_cookie(state, origin)


def _origin_binding(origin: str) -> str:
    return hashlib.sha256(origin.encode("utf-8")).hexdigest()


def session_cookie_name(origin: str, primary_origin: str) -> str:
    """Return the host-only cookie name for one configured GUI origin."""
    if origin == primary_origin:
        return "asr_studio_session"
    return f"asr_studio_session_{_origin_binding(origin)[:24]}"


def _create_session_cookie(state: AuthState, origin: str | None) -> str:
    nonce = _b64url_encode(secrets.token_bytes(24))
    if origin is None:
        payload = f"v1:{state.generation}:{nonce}".encode("ascii")
    else:
        payload = f"v2:{state.generation}:{_origin_binding(origin)}:{nonce}".encode("ascii")
    signing_key = _b64url_decode(state.signing_secret)
    signature = hmac.new(signing_key, payload, hashlib.sha256).digest()
    return f"{_b64url_encode(payload)}.{_b64url_encode(signature)}"


def session_is_valid(
    cookie_value: str | None,
    state: AuthState,
    origin: str | None = None,
    *,
    allow_legacy: bool | None = None,
) -> bool:
    if (
        not isinstance(cookie_value, str)
        or not cookie_value
        or len(cookie_value) > 512
        or cookie_value.count(".") != 1
    ):
        return False
    if allow_legacy is None:
        allow_legacy = origin is None
    payload_part, signature_part = cookie_value.split(".", maxsplit=1)
    try:
        payload = _b64url_decode(payload_part)
        signature = _b64url_decode(signature_part)
        fields = payload.decode("ascii").split(":")
        if len(fields) not in {3, 4} or fields[0] not in {"v1", "v2"}:
            return False
        generation = int(fields[1])
        if fields[0] == "v1":
            if len(fields) != 3 or not allow_legacy:
                return False
        elif (
            len(fields) != 4
            or origin is None
            or not hmac.compare_digest(fields[2], _origin_binding(origin))
        ):
            return False
        expected_signature = hmac.new(
            _b64url_decode(state.signing_secret), payload, hashlib.sha256
        ).digest()
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return False
    signature_valid = hmac.compare_digest(signature, expected_signature)
    return signature_valid and generation == state.generation
