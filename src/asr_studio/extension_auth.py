"""Durable authentication state for the production browser extension API."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from asr_studio.storage import database_path

EXTENSION_ID = "fgkjjpoeokhajpiagepfcpmgfapilinf"
EXTENSION_PROTOCOL = "asr-extension-v1"
EXTENSION_API_VERSION = 1
CODE_LIFETIME = timedelta(minutes=10)
SESSION_LIFETIME = timedelta(minutes=15)
PAIRING_RATE_WINDOW = timedelta(minutes=10)
MAX_PAIRING_ATTEMPTS = 32
MAX_PENDING_DEVICE_RECORDS = 64
MAX_ACTIVE_SESSIONS_PER_DEVICE = 8
MAX_CODE_BYTES = 256
MAX_DEVICE_ID_BYTES = 128
MAX_RECORD_ID_BYTES = 128
MAX_BEARER_BYTES = 512
DEVICE_LIFETIMES = frozenset({"7d", "30d", "90d", "never"})
DEVICE_LIFETIME_DURATIONS = {
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
    "90d": timedelta(days=90),
}


class ExtensionAuthError(RuntimeError):
    """A safe error that can cross the extension API boundary."""

    def __init__(self, code: str, message: str, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class _PrivateAuthState:
    backend_id: str
    hmac_secret: bytes


class _PrivateAuthStateStore:
    """Keep the extension key outside the shared data volume and UI auth file."""

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        self.state_path = self.directory / "extension-auth.json"
        self.lock_path = self.directory / ".extension-auth.lock"

    @contextmanager
    def _locked(self, *, exclusive: bool) -> Iterator[None]:
        import fcntl

        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.directory, 0o700)
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            lock_fd = os.open(self.lock_path, flags, 0o600)
        except OSError as exc:
            raise ExtensionAuthError(
                "authentication_unavailable",
                "Extension authentication is unavailable.",
                503,
            ) from exc
        try:
            os.fchmod(lock_fd, 0o600)
            fcntl.flock(lock_fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            yield
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)

    def _read_locked(self) -> _PrivateAuthState:
        if self.state_path.is_symlink():
            raise ExtensionAuthError(
                "authentication_unavailable",
                "Extension authentication is unavailable.",
                503,
            )
        try:
            document = json.loads(self.state_path.read_text(encoding="utf-8"))
            backend_id = document["backend_id"]
            hmac_secret = bytes.fromhex(document["hmac_secret"])
            if (
                document.get("version") != 1
                or not isinstance(backend_id, str)
                or len(backend_id) != 32
                or any(character not in "0123456789abcdef" for character in backend_id)
                or len(hmac_secret) != 32
            ):
                raise ValueError("invalid extension authentication state")
            os.chmod(self.state_path, 0o600)
            return _PrivateAuthState(backend_id, hmac_secret)
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ExtensionAuthError(
                "authentication_unavailable",
                "Extension authentication is unavailable.",
                503,
            ) from exc

    def _write_locked(self, state: _PrivateAuthState) -> None:
        document = json.dumps(
            {
                "version": 1,
                "backend_id": state.backend_id,
                "hmac_secret": state.hmac_secret.hex(),
            },
            separators=(",", ":"),
        )
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".extension-auth-",
            dir=self.directory,
        )
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as temporary_file:
                temporary_file.write(document)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.replace(temporary_name, self.state_path)
            os.chmod(self.state_path, 0o600)
            directory_fd = os.open(
                self.directory,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError as exc:
            with suppress(FileNotFoundError):
                os.unlink(temporary_name)
            raise ExtensionAuthError(
                "authentication_unavailable",
                "Extension authentication is unavailable.",
                503,
            ) from exc

    def ensure(self) -> _PrivateAuthState:
        with self._locked(exclusive=True):
            if not self.state_path.exists() and not self.state_path.is_symlink():
                self._write_locked(
                    _PrivateAuthState(secrets.token_hex(16), secrets.token_bytes(32))
                )
            return self._read_locked()

    def load(self) -> _PrivateAuthState:
        with self._locked(exclusive=False):
            return self._read_locked()


@dataclass(frozen=True, slots=True)
class PairingCode:
    code: str
    expires_at: str
    device_lifetime: str


@dataclass(frozen=True, slots=True)
class DeviceCredential:
    token: str
    expires_at: str | None
    device_lifetime: str
    backend_id: str


@dataclass(frozen=True, slots=True)
class SessionCredential:
    token: str
    expires_at: str
    backend_id: str


@dataclass(frozen=True, slots=True)
class ExtensionSessionIdentity:
    """The server-owned identity that #218 may use for ingest authorization."""

    device_record_id: str
    owner_id: str
    device_id: str
    server_url: str
    generation: int
    scope: str


def _now(value: datetime | None = None) -> datetime:
    current = value or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return current.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return _now(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    parsed = datetime.fromisoformat(normalized)
    return _now(parsed)


def _expired(value: str | None, current: datetime) -> bool:
    return value is not None and _parse_timestamp(value) <= current


def _secret_digest(secret: bytes, purpose: str, value: str) -> bytes:
    return hmac.new(
        secret,
        purpose.encode("ascii") + b"\0" + value.encode("utf-8"),
        hashlib.sha256,
    ).digest()


def _safe_token(value: str, *, limit: int) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value.encode("utf-8")) <= limit
        and all(0x21 <= ord(character) <= 0x7E for character in value)
    )


def _safe_device_id(value: Any) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value.encode("utf-8")) <= MAX_DEVICE_ID_BYTES
        and all(character.isprintable() and not character.isspace() for character in value)
    )


def _safe_record_id(value: Any) -> bool:
    return _safe_token(value, limit=MAX_RECORD_ID_BYTES)


def _lifetime(value: Any) -> str:
    if not isinstance(value, str) or value not in DEVICE_LIFETIMES:
        raise ExtensionAuthError(
            "invalid_request",
            "Choose a supported device lifetime.",
            400,
        )
    return value


def _generic_code_error() -> ExtensionAuthError:
    return ExtensionAuthError(
        "invalid_pairing_code",
        "The pairing code is invalid or expired.",
        401,
    )


def _generic_device_error() -> ExtensionAuthError:
    return ExtensionAuthError(
        "invalid_device_credential",
        "The device credential is invalid or no longer active.",
        401,
    )


def _generic_session_error() -> ExtensionAuthError:
    return ExtensionAuthError(
        "invalid_session_credential",
        "The session credential is invalid or no longer active.",
        401,
    )


class ExtensionAuthService:
    """Issue and check credentials without exposing database secrets."""

    def __init__(
        self,
        data_dir: Path,
        server_urls: tuple[str, ...],
        *,
        auth_dir: Path | None = None,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.server_urls = tuple(server_urls)
        self.private_state = _PrivateAuthStateStore(
            Path(auth_dir) if auth_dir is not None else self.data_dir.parent / "auth"
        )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(database_path(self.data_dir), timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _state(self) -> _PrivateAuthState:
        return self.private_state.load()

    def ensure(self) -> str:
        # Persist the public identity after the private file is durable. A crash
        # before the DB commit can reuse that file; a later loss cannot bootstrap
        # a different installation, even if no device has been paired yet.
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            installation = connection.execute(
                "SELECT backend_id FROM extension_auth_installation WHERE id = 1"
            ).fetchone()
            has_devices = (
                connection.execute("SELECT 1 FROM extension_devices LIMIT 1").fetchone() is not None
            )
            state = (
                self.private_state.load()
                if installation is not None or has_devices
                else self.private_state.ensure()
            )
            if installation is not None and installation["backend_id"] != state.backend_id:
                raise ExtensionAuthError(
                    "authentication_unavailable",
                    "Extension authentication is unavailable.",
                    503,
                )
            if installation is None:
                connection.execute(
                    "INSERT INTO extension_auth_installation(id, backend_id) VALUES (1, ?)",
                    (state.backend_id,),
                )
            connection.commit()
            return state.backend_id
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def backend_id(self) -> str:
        return self._state().backend_id

    def _require_server_url(self, server_url: str) -> None:
        if server_url not in self.server_urls:
            raise ExtensionAuthError(
                "invalid_endpoint",
                "Use a configured server endpoint.",
                400,
            )

    def create_pairing_code(
        self,
        *,
        device_lifetime: str,
        device_record_id: str | None,
        server_url: str | None = None,
        now: datetime | None = None,
    ) -> PairingCode:
        lifetime = _lifetime(device_lifetime)
        if device_record_id is not None and not _safe_record_id(device_record_id):
            raise ExtensionAuthError("invalid_request", "The device record is invalid.", 400)
        if server_url is not None:
            self._require_server_url(server_url)
        current = _now(now)
        created_at = _timestamp(current)
        expires_at = _timestamp(current + CODE_LIFETIME)
        raw_code = secrets.token_urlsafe(18)
        record_id = device_record_id or secrets.token_hex(16)
        owner_id = secrets.token_hex(16)

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            state = self._state()
            if device_record_id is None:
                pending_count = connection.execute(
                    "SELECT COUNT(*) FROM extension_devices WHERE device_token_hash IS NULL "
                    "AND revoked_at IS NULL AND pending_expires_at > ?",
                    (created_at,),
                ).fetchone()[0]
                if pending_count >= MAX_PENDING_DEVICE_RECORDS:
                    raise ExtensionAuthError(
                        "pending_limit",
                        "Too many pending devices. Activate or revoke one first.",
                        429,
                    )
                connection.execute(
                    "INSERT INTO extension_devices("
                    "id, owner_id, server_url, device_lifetime, pending_expires_at, "
                    "created_at, generation) VALUES (?, ?, ?, ?, ?, ?, 0)",
                    (record_id, owner_id, server_url, lifetime, expires_at, created_at),
                )
            else:
                device = connection.execute(
                    "SELECT id, generation, device_token_hash FROM extension_devices WHERE id = ?",
                    (device_record_id,),
                ).fetchone()
                if device is None:
                    raise ExtensionAuthError("not_found", "Device record not found.", 404)
                if device["generation"] == 0 or device["device_token_hash"] is None:
                    connection.execute(
                        "UPDATE extension_devices SET server_url = ?, device_lifetime = ?, "
                        "pending_expires_at = ? WHERE id = ?",
                        (server_url, lifetime, expires_at, device_record_id),
                    )
            connection.execute(
                "UPDATE extension_pairing_codes SET invalidated_at = ? "
                "WHERE device_record_id = ? AND consumed_at IS NULL AND invalidated_at IS NULL",
                (created_at, record_id),
            )
            connection.execute(
                "INSERT INTO extension_pairing_codes("
                "id, device_record_id, code_hash, server_url, device_lifetime, "
                "created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    secrets.token_hex(16),
                    record_id,
                    _secret_digest(state.hmac_secret, "pairing-code", raw_code),
                    server_url,
                    lifetime,
                    created_at,
                    expires_at,
                ),
            )
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()
        return PairingCode(raw_code, expires_at, lifetime)

    def _device_status(self, row: sqlite3.Row, current: datetime) -> str:
        if row["revoked_at"] is not None:
            return "revoked"
        if row["device_token_hash"] is None:
            return "expired" if _expired(row["pending_expires_at"], current) else "pending"
        if row["pending_expires_at"] is not None:
            return "expired" if _expired(row["pending_expires_at"], current) else "pending"
        return "expired" if _expired(row["device_expires_at"], current) else "active"

    def list_devices(self, *, now: datetime | None = None) -> list[dict[str, Any]]:
        current = _now(now)
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT id, device_id, server_url, device_lifetime, device_expires_at, "
                "created_at, last_seen_at, revoked_at, device_token_hash, pending_expires_at "
                "FROM extension_devices ORDER BY created_at ASC, id ASC"
            ).fetchall()
        finally:
            connection.close()
        return [
            {
                "id": row["id"],
                "device_id": row["device_id"],
                "server_url": row["server_url"],
                "device_lifetime": row["device_lifetime"],
                "device_expires_at": row["device_expires_at"],
                "created_at": row["created_at"],
                "last_seen_at": row["last_seen_at"],
                "revoked_at": row["revoked_at"],
                "status": self._device_status(row, current),
            }
            for row in rows
        ]

    def revoke_device(self, device_record_id: str, *, now: datetime | None = None) -> None:
        if not _safe_record_id(device_record_id):
            raise ExtensionAuthError("not_found", "Device record not found.", 404)
        current = _timestamp(_now(now))
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT revoked_at FROM extension_devices WHERE id = ?", (device_record_id,)
            ).fetchone()
            if row is None:
                raise ExtensionAuthError("not_found", "Device record not found.", 404)
            if row["revoked_at"] is None:
                connection.execute(
                    "UPDATE extension_devices SET revoked_at = ? WHERE id = ?",
                    (current, device_record_id),
                )
            connection.execute(
                "UPDATE extension_pairing_codes SET invalidated_at = ? "
                "WHERE device_record_id = ? AND consumed_at IS NULL AND invalidated_at IS NULL",
                (current, device_record_id),
            )
            connection.execute(
                "UPDATE extension_sessions SET revoked_at = ? "
                "WHERE device_record_id = ? AND revoked_at IS NULL",
                (current, device_record_id),
            )
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def _record_pairing_attempt(self, current: datetime) -> None:
        """Commit the attempt counter before validating the submitted secret."""
        current_text = _timestamp(current)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            rate = connection.execute(
                "SELECT window_started_at, attempt_count FROM extension_pairing_rate WHERE id = 1"
            ).fetchone()
            if rate is None:
                raise ExtensionAuthError(
                    "authentication_unavailable",
                    "Extension authentication is unavailable.",
                    503,
                )
            rate_started = _parse_timestamp(rate["window_started_at"])
            if current - rate_started >= PAIRING_RATE_WINDOW:
                connection.execute(
                    "UPDATE extension_pairing_rate SET window_started_at = ?, attempt_count = 1 "
                    "WHERE id = 1",
                    (current_text,),
                )
            elif int(rate["attempt_count"]) >= MAX_PAIRING_ATTEMPTS:
                raise ExtensionAuthError(
                    "pairing_rate_limited",
                    "Too many pairing attempts. Try again later.",
                    429,
                )
            else:
                connection.execute(
                    "UPDATE extension_pairing_rate SET attempt_count = attempt_count + 1 "
                    "WHERE id = 1"
                )
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def pair(
        self,
        *,
        code: str,
        device_id: str,
        server_url: str,
        now: datetime | None = None,
    ) -> DeviceCredential:
        self._require_server_url(server_url)
        current = _now(now)
        current_text = _timestamp(current)
        self._record_pairing_attempt(current)
        if not _safe_token(code, limit=MAX_CODE_BYTES) or not _safe_device_id(device_id):
            raise _generic_code_error()
        connection = self._connect()
        raw_token = secrets.token_urlsafe(32)
        try:
            connection.execute("BEGIN IMMEDIATE")
            state = self._state()
            code_hash = _secret_digest(state.hmac_secret, "pairing-code", code)
            pairing = connection.execute(
                "SELECT p.id, p.device_record_id, p.server_url, p.device_lifetime, p.expires_at, "
                "p.consumed_at, p.invalidated_at, d.generation, d.revoked_at "
                "FROM extension_pairing_codes AS p JOIN extension_devices AS d "
                "ON d.id = p.device_record_id WHERE p.code_hash = ?",
                (code_hash,),
            ).fetchone()
            if pairing is None or pairing["server_url"] not in {None, server_url}:
                raise _generic_code_error()
            if pairing["consumed_at"] is not None:
                raise ExtensionAuthError(
                    "pairing_code_replayed",
                    "The pairing code was already used.",
                    409,
                )
            if pairing["invalidated_at"] is not None or _expired(pairing["expires_at"], current):
                raise _generic_code_error()
            device_expires_at = None
            if pairing["device_lifetime"] != "never":
                device_expires_at = _timestamp(
                    current + DEVICE_LIFETIME_DURATIONS[pairing["device_lifetime"]]
                )
            generation = max(1, int(pairing["generation"]) + 1)
            connection.execute(
                "UPDATE extension_pairing_codes SET consumed_at = ? WHERE id = ?",
                (current_text, pairing["id"]),
            )
            connection.execute(
                "UPDATE extension_sessions SET revoked_at = ? "
                "WHERE device_record_id = ? AND revoked_at IS NULL",
                (current_text, pairing["device_record_id"]),
            )
            connection.execute(
                "UPDATE extension_devices SET device_id = ?, server_url = ?, "
                "device_lifetime = ?, device_expires_at = ?, pending_expires_at = ?, "
                "last_seen_at = NULL, revoked_at = NULL, generation = ?, device_token_hash = ? "
                "WHERE id = ?",
                (
                    device_id,
                    server_url,
                    pairing["device_lifetime"],
                    device_expires_at,
                    pairing["expires_at"],
                    generation,
                    _secret_digest(state.hmac_secret, "device-token", raw_token),
                    pairing["device_record_id"],
                ),
            )
            connection.commit()
            return DeviceCredential(
                raw_token,
                device_expires_at,
                pairing["device_lifetime"],
                state.backend_id,
            )
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def create_session(
        self,
        *,
        device_token: str,
        server_url: str,
        now: datetime | None = None,
    ) -> SessionCredential:
        if not _safe_token(device_token, limit=MAX_BEARER_BYTES):
            raise _generic_device_error()
        self._require_server_url(server_url)
        current = _now(now)
        current_text = _timestamp(current)
        expires_at = _timestamp(current + SESSION_LIFETIME)
        raw_session = secrets.token_urlsafe(32)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            state = self._state()
            row = connection.execute(
                "SELECT id, server_url, device_expires_at, pending_expires_at, "
                "revoked_at, generation "
                "FROM extension_devices WHERE device_token_hash = ?",
                (_secret_digest(state.hmac_secret, "device-token", device_token),),
            ).fetchone()
            if (
                row is None
                or row["server_url"] != server_url
                or row["revoked_at"] is not None
                or int(row["generation"]) < 1
                or _expired(row["pending_expires_at"], current)
                or _expired(row["device_expires_at"], current)
            ):
                raise _generic_device_error()
            connection.execute(
                "INSERT INTO extension_sessions("
                "token_hash, device_record_id, device_generation, server_url, scope, "
                "created_at, expires_at) VALUES (?, ?, ?, ?, 'ingest', ?, ?)",
                (
                    _secret_digest(state.hmac_secret, "session-token", raw_session),
                    row["id"],
                    row["generation"],
                    server_url,
                    current_text,
                    expires_at,
                ),
            )
            connection.execute(
                "DELETE FROM extension_sessions WHERE revoked_at IS NOT NULL OR expires_at <= ?",
                (current_text,),
            )
            stale_sessions = connection.execute(
                "SELECT token_hash FROM extension_sessions WHERE device_record_id = ? "
                "ORDER BY created_at DESC, rowid DESC LIMIT -1 OFFSET ?",
                (row["id"], MAX_ACTIVE_SESSIONS_PER_DEVICE),
            ).fetchall()
            connection.executemany(
                "DELETE FROM extension_sessions WHERE token_hash = ?",
                ((stale["token_hash"],) for stale in stale_sessions),
            )
            connection.execute(
                "UPDATE extension_devices SET last_seen_at = ?, pending_expires_at = NULL "
                "WHERE id = ?",
                (current_text, row["id"]),
            )
            connection.commit()
            return SessionCredential(raw_session, expires_at, state.backend_id)
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def authorize_session(
        self,
        *,
        session_token: str,
        server_url: str,
        now: datetime | None = None,
    ) -> ExtensionSessionIdentity:
        if not _safe_token(session_token, limit=MAX_BEARER_BYTES):
            raise _generic_session_error()
        self._require_server_url(server_url)
        current = _now(now)
        current_text = _timestamp(current)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            state = self._state()
            row = connection.execute(
                "SELECT s.device_record_id, s.device_generation, s.server_url, s.scope, "
                "s.expires_at, s.revoked_at, d.owner_id, d.device_id, "
                "d.server_url AS d_server_url, "
                "d.generation, d.revoked_at AS d_revoked_at, d.device_expires_at, "
                "d.pending_expires_at "
                "FROM extension_sessions AS s JOIN extension_devices AS d "
                "ON d.id = s.device_record_id WHERE s.token_hash = ?",
                (_secret_digest(state.hmac_secret, "session-token", session_token),),
            ).fetchone()
            if (
                row is None
                or row["server_url"] != server_url
                or row["d_server_url"] != server_url
                or row["scope"] != "ingest"
                or row["revoked_at"] is not None
                or row["d_revoked_at"] is not None
                or int(row["device_generation"]) != int(row["generation"])
                or _expired(row["expires_at"], current)
                or _expired(row["device_expires_at"], current)
                or row["pending_expires_at"] is not None
                or not _safe_device_id(row["device_id"])
            ):
                raise _generic_session_error()
            connection.execute(
                "UPDATE extension_devices SET last_seen_at = ? WHERE id = ?",
                (current_text, row["device_record_id"]),
            )
            connection.commit()
            return ExtensionSessionIdentity(
                device_record_id=row["device_record_id"],
                owner_id=row["owner_id"],
                device_id=row["device_id"],
                server_url=server_url,
                generation=int(row["generation"]),
                scope=row["scope"],
            )
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()


def authorize_extension_session(
    data_dir: Path,
    session_token: str,
    server_url: str | None = None,
    *,
    endpoint: str | None = None,
    auth_dir: Path | None = None,
    now: datetime | None = None,
) -> ExtensionSessionIdentity:
    """Check a session bearer and return only server-owned ingest identity."""
    selected_endpoint = endpoint or server_url
    if selected_endpoint is None:
        raise ValueError("server_url or endpoint is required")
    return ExtensionAuthService(
        Path(data_dir),
        (selected_endpoint,),
        auth_dir=auth_dir,
    ).authorize_session(
        session_token=session_token,
        server_url=selected_endpoint,
        now=now,
    )
