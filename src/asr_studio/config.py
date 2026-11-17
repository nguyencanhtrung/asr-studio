"""Runtime configuration with strict GUI-origin and callback URL validation."""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_PUBLIC_URL = "http://127.0.0.1:8765"
DEFAULT_PROVIDER_DIR = Path("/var/lib/asr-studio/provider")
DEFAULT_INSTALL_DIR = Path("/var/lib/asr-studio/install")
CHATGPT_CALLBACK_PATH = "/auth/chatgpt/callback"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
MAX_WEB_ORIGINS = 8
MAX_EXTENSION_URLS = 8
MAX_ORIGIN_BYTES = 256
DEFAULT_MAX_UPLOAD_BYTES = 4 * 1024**3
DEFAULT_MAX_RECORDING_SECONDS = 4 * 60 * 60
DEFAULT_UPLOAD_CHUNK_BYTES = 64 * 1024**2
EXTENSION_MAX_CHUNK_BYTES = 8 * 1024**2
EXTENSION_MAX_CHUNKS = 65_536
EXTENSION_MAX_LOCAL_BYTES = 512 * 1024**2
EXTENSION_MAX_RECORDING_BYTES = 4 * 1024**3
EXTENSION_MAX_ACTIVE_SESSIONS = 64
EXTENSION_FINALIZE_ENTRY_BYTES = 160
EXTENSION_FINALIZE_ENVELOPE_BYTES = 16 * 1024
EXTENSION_CHUNK_BODY_IDLE_SECONDS = 30.0
EXTENSION_CHUNK_BODY_TOTAL_SECONDS = 300.0
EXTENSION_MEDIA_WALL_SECONDS = 30 * 60
EXTENSION_MEDIA_OUTPUT_BYTES = 512 * 1024**2
EXTENSION_MEDIA_DISK_RESERVE_BYTES = 64 * 1024**2
DEFAULT_ASR_GPU_MODEL_PATH = Path("/models/qwen3-asr-1.7b")
DEFAULT_ASR_CPU_MODEL_PATH = Path("/models/whisper-small")
DEFAULT_ASR_PHOWHISPER_MODEL_PATH = Path("/models/pho-whisper-large")
DEFAULT_ASR_QWEN_MODEL_PATH = Path("/models/qwen3-asr-1.7b")
DEFAULT_DIARIZATION_MODEL_PATH = Path("/models/speaker-diarization-community-1")
ASR_DEVICES = frozenset({"auto", "cpu", "cuda"})
ASR_COMPUTE_TYPES = frozenset(
    {"default", "float16", "float32", "int8", "int8_float16", "int8_float32"}
)


def _url_has_unsafe_text(raw_url: str) -> bool:
    return (
        any(ord(character) < 0x20 or ord(character) == 0x7F for character in raw_url)
        or any(character.isspace() for character in raw_url)
        or "\\" in raw_url
    )


def _valid_hostname(hostname: str) -> bool:
    if not hostname or hostname.endswith(".") or hostname.startswith(".") or ".." in hostname:
        return False
    if ":" in hostname:
        try:
            ipaddress.IPv6Address(hostname)
        except ValueError:
            return False
        return True
    labels = hostname.split(".")
    return all(
        label
        and not label.startswith("-")
        and not label.endswith("-")
        and all(character.isalnum() or character == "-" for character in label)
        for label in labels
    )


def _normalized_server_origin(raw_url: str, *, error_message: str) -> str:
    if (
        not isinstance(raw_url, str)
        or not raw_url
        or len(raw_url.encode("utf-8")) > MAX_ORIGIN_BYTES
        or _url_has_unsafe_text(raw_url)
    ):
        raise ValueError(error_message)
    try:
        parsed = urlsplit(raw_url)
        hostname = parsed.hostname
    except (TypeError, ValueError) as exc:
        raise ValueError(error_message) from exc
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or "*" in parsed.netloc
        or not _valid_hostname(hostname.lower())
    ):
        raise ValueError(error_message)
    normalized_hostname = hostname.lower()
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(error_message) from exc
    if port is not None and not 1 <= port <= 65535:
        raise ValueError(error_message)
    loopback = normalized_hostname in LOOPBACK_HOSTS
    scheme = parsed.scheme.lower()
    if scheme == "http" and not loopback:
        raise ValueError(error_message)
    authority = f"[{normalized_hostname}]" if ":" in normalized_hostname else normalized_hostname
    default_port = 443 if scheme == "https" else 80
    if port is not None and port != default_port:
        authority = f"{authority}:{port}"
    return f"{scheme}://{authority}"


def _normalized_extension_url(raw_url: str) -> str:
    return _normalized_server_origin(
        raw_url,
        error_message="ASR_PUBLIC_URL and ASR_EXTENSION_URLS must be strict server origins",
    )


def _normalized_web_url(raw_url: str) -> str:
    return _normalized_server_origin(
        raw_url,
        error_message="ASR_PUBLIC_URL and ASR_WEB_ORIGINS must be strict GUI origins",
    )


def _origin_host_authority(origin: str) -> str:
    parsed = urlsplit(origin)
    hostname = (parsed.hostname or "").lower()
    authority = f"[{hostname}]" if ":" in hostname else hostname
    default_port = 443 if parsed.scheme == "https" else 80
    port = parsed.port or default_port
    return authority if port == default_port else f"{authority}:{port}"


def _extension_authority(url: str) -> tuple[str, int]:
    parsed = urlsplit(url)
    hostname = (parsed.hostname or "").lower()
    default_port = 443 if parsed.scheme == "https" else 80
    return hostname, parsed.port or default_port


def _host_header_parts(host_header: str | None) -> tuple[str, int | None] | None:
    if (
        not isinstance(host_header, str)
        or not host_header
        or host_header.endswith(":")
        or _url_has_unsafe_text(host_header)
    ):
        return None
    try:
        parsed = urlsplit(f"http://{host_header}")
        hostname = parsed.hostname
        port = parsed.port
    except (TypeError, ValueError):
        return None
    if (
        hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        return None
    normalized_hostname = hostname.lower()
    if not _valid_hostname(normalized_hostname):
        return None
    return normalized_hostname, port


def _host_matches_origin(host_header: str | None, origin: str) -> bool:
    parts = _host_header_parts(host_header)
    if parts is None:
        return False
    hostname, port = parts
    parsed = urlsplit(origin)
    expected_hostname = (parsed.hostname or "").lower()
    default_port = 443 if parsed.scheme == "https" else 80
    expected_port = parsed.port or default_port
    if hostname != expected_hostname:
        return False
    return (port is None and expected_port == default_port) or port == expected_port


def _bool_from_env(name: str, default: bool) -> bool:
    raw_value = os.environ.get(name)
    if raw_value is None:
        return default
    normalized = raw_value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean value")


def _positive_int_from_env(name: str, default: int) -> int:
    raw_value = os.environ.get(name)
    if raw_value is None:
        return default
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def extension_finalize_body_bytes(max_chunks: int = EXTENSION_MAX_CHUNKS) -> int:
    """Return the finite JSON envelope bound for one chunk-manifest submission."""
    if isinstance(max_chunks, bool) or max_chunks < 1:
        raise ValueError("max_chunks must be positive")
    return EXTENSION_FINALIZE_ENVELOPE_BYTES + max_chunks * EXTENSION_FINALIZE_ENTRY_BYTES


def _validate_public_url(raw_url: str) -> str:
    return _normalized_server_origin(
        raw_url,
        error_message="ASR_PUBLIC_URL must be a loopback http(s) origin or an HTTPS public origin",
    )


def _validate_chatgpt_callback_url(raw_url: str) -> str:
    if not isinstance(raw_url, str) or any(
        ord(character) < 0x20 or ord(character) == 0x7F for character in raw_url
    ):
        raise ValueError(
            "ASR_CHATGPT_CALLBACK_URL must be an HTTP 127.0.0.1 callback URL at "
            f"{CHATGPT_CALLBACK_PATH}"
        )
    try:
        parsed = urlsplit(raw_url)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "ASR_CHATGPT_CALLBACK_URL must be an HTTP 127.0.0.1 callback URL at "
            f"{CHATGPT_CALLBACK_PATH}"
        ) from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != CHATGPT_CALLBACK_PATH
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "ASR_CHATGPT_CALLBACK_URL must be an HTTP 127.0.0.1 callback URL at "
            f"{CHATGPT_CALLBACK_PATH}"
        )
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("ASR_CHATGPT_CALLBACK_URL has an invalid port") from exc
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("ASR_CHATGPT_CALLBACK_URL has an invalid port")
    return f"http://{parsed.netloc}{CHATGPT_CALLBACK_PATH}"


@dataclass(frozen=True, slots=True)
class Settings:
    data_dir: Path
    auth_dir: Path
    public_url: str = DEFAULT_PUBLIC_URL
    web_origins: tuple[str, ...] = ()
    cookie_secure: bool = False
    max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES
    upload_chunk_bytes: int = DEFAULT_UPLOAD_CHUNK_BYTES
    max_recording_seconds: int = DEFAULT_MAX_RECORDING_SECONDS
    asr_model_path: Path | None = None
    asr_device: str = "auto"
    asr_compute_type: str | None = None
    asr_gpu_model_path: Path = DEFAULT_ASR_GPU_MODEL_PATH
    asr_cpu_model_path: Path = DEFAULT_ASR_CPU_MODEL_PATH
    asr_phowhisper_model_path: Path = DEFAULT_ASR_PHOWHISPER_MODEL_PATH
    asr_qwen_model_path: Path = DEFAULT_ASR_QWEN_MODEL_PATH
    asr_gpu_compute_type: str = "float16"
    asr_cpu_compute_type: str = "int8"
    diarization_model_path: Path = DEFAULT_DIARIZATION_MODEL_PATH
    provider_dir: Path | None = None
    install_dir: Path | None = None
    chatgpt_callback_url: str | None = None
    extension_urls: tuple[str, ...] = ()
    extension_max_staging_bytes: int = EXTENSION_MAX_LOCAL_BYTES
    extension_max_active_sessions: int = EXTENSION_MAX_ACTIVE_SESSIONS

    def __post_init__(self) -> None:
        object.__setattr__(self, "data_dir", Path(self.data_dir))
        object.__setattr__(self, "auth_dir", Path(self.auth_dir))
        provider_dir = self.provider_dir
        if provider_dir is None:
            provider_dir = Path(self.data_dir).parent / "provider"
        object.__setattr__(self, "provider_dir", Path(provider_dir))
        object.__setattr__(
            self,
            "install_dir",
            Path(self.install_dir or Path(self.data_dir).parent / "install"),
        )
        if self.asr_model_path is not None:
            object.__setattr__(self, "asr_model_path", Path(self.asr_model_path))
        object.__setattr__(self, "asr_gpu_model_path", Path(self.asr_gpu_model_path))
        object.__setattr__(self, "asr_cpu_model_path", Path(self.asr_cpu_model_path))
        object.__setattr__(self, "asr_phowhisper_model_path", Path(self.asr_phowhisper_model_path))
        object.__setattr__(self, "asr_qwen_model_path", Path(self.asr_qwen_model_path))
        object.__setattr__(self, "diarization_model_path", Path(self.diarization_model_path))
        object.__setattr__(self, "public_url", _validate_public_url(self.public_url))
        web_origins = self.web_origins
        if isinstance(web_origins, str):
            web_origins = tuple(web_origins.split(",")) if web_origins else ()
        if web_origins is None or not isinstance(web_origins, tuple):
            raise ValueError("web_origins must be a tuple of GUI origins")
        if len(web_origins) > MAX_WEB_ORIGINS:
            raise ValueError(f"ASR_WEB_ORIGINS supports at most {MAX_WEB_ORIGINS} origins")
        normalized_web_origins = tuple(_normalized_web_url(origin) for origin in web_origins)
        web_origins_all = (self.public_url, *normalized_web_origins)
        if len(set(web_origins_all)) != len(web_origins_all):
            raise ValueError("ASR_WEB_ORIGINS contains a duplicate GUI origin")
        effective_authorities: set[tuple[str, int]] = set()
        bare_host_names: set[str] = set()
        for origin in web_origins_all:
            parsed = urlsplit(origin)
            hostname = (parsed.hostname or "").lower()
            default_port = 443 if parsed.scheme == "https" else 80
            effective_port = parsed.port or default_port
            effective_key = (hostname, effective_port)
            if effective_key in effective_authorities:
                raise ValueError("ASR_WEB_ORIGINS contains an ambiguous Host authority")
            if effective_port == default_port and hostname in bare_host_names:
                raise ValueError("ASR_WEB_ORIGINS contains an ambiguous bare Host authority")
            effective_authorities.add(effective_key)
            if effective_port == default_port:
                bare_host_names.add(hostname)
        object.__setattr__(self, "web_origins", normalized_web_origins)
        aliases = self.extension_urls
        if isinstance(aliases, str):
            aliases = tuple(aliases.split(",")) if aliases else ()
        if aliases is None or not isinstance(aliases, tuple):
            raise ValueError("extension_urls must be a tuple of server origins")
        if len(aliases) > MAX_EXTENSION_URLS:
            raise ValueError(f"ASR_EXTENSION_URLS supports at most {MAX_EXTENSION_URLS} origins")
        normalized_default = _normalized_extension_url(self.public_url)
        normalized_aliases = tuple(_normalized_extension_url(alias) for alias in aliases)
        authorities = {_extension_authority(normalized_default)}
        host_headers = {_origin_host_authority(normalized_default)}
        for alias in normalized_aliases:
            authority = _extension_authority(alias)
            host_header = _origin_host_authority(alias)
            if authority in authorities or host_header in host_headers:
                raise ValueError("ASR_EXTENSION_URLS contains an ambiguous endpoint authority")
            authorities.add(authority)
            host_headers.add(host_header)
        object.__setattr__(self, "extension_urls", normalized_aliases)
        callback_url = self.chatgpt_callback_url
        if callback_url is None and urlsplit(self.public_url).scheme == "http":
            parsed_public_url = urlsplit(self.public_url)
            callback_port = parsed_public_url.port or 80
            callback_url = f"http://127.0.0.1:{callback_port}{CHATGPT_CALLBACK_PATH}"
        object.__setattr__(
            self,
            "chatgpt_callback_url",
            _validate_chatgpt_callback_url(callback_url) if callback_url else None,
        )
        if isinstance(self.max_upload_bytes, bool) or self.max_upload_bytes < 1:
            raise ValueError("max_upload_bytes must be a positive integer")
        if (
            isinstance(self.upload_chunk_bytes, bool)
            or self.upload_chunk_bytes < 1
            or self.upload_chunk_bytes > DEFAULT_UPLOAD_CHUNK_BYTES
        ):
            raise ValueError(
                f"upload_chunk_bytes must be between 1 and {DEFAULT_UPLOAD_CHUNK_BYTES}"
            )
        if isinstance(self.max_recording_seconds, bool) or self.max_recording_seconds < 1:
            raise ValueError("max_recording_seconds must be a positive integer")
        if (
            isinstance(self.extension_max_staging_bytes, bool)
            or self.extension_max_staging_bytes < 1
        ):
            raise ValueError("extension_max_staging_bytes must be a positive integer")
        if (
            isinstance(self.extension_max_active_sessions, bool)
            or self.extension_max_active_sessions < 1
        ):
            raise ValueError("extension_max_active_sessions must be a positive integer")
        if self.asr_device not in ASR_DEVICES:
            raise ValueError("asr_device must be auto, cpu, or cuda")
        for value in (
            self.asr_compute_type,
            self.asr_gpu_compute_type,
            self.asr_cpu_compute_type,
        ):
            if value is not None and value not in ASR_COMPUTE_TYPES:
                raise ValueError("asr_compute_type is not supported")

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            data_dir=Path(os.environ.get("ASR_DATA_DIR", "/var/lib/asr-studio/data")),
            auth_dir=Path(os.environ.get("ASR_AUTH_DIR", "/var/lib/asr-studio/auth")),
            provider_dir=Path(os.environ.get("ASR_PROVIDER_DIR", str(DEFAULT_PROVIDER_DIR))),
            install_dir=Path(os.environ.get("ASR_INSTALL_DIR", str(DEFAULT_INSTALL_DIR))),
            public_url=os.environ.get("ASR_PUBLIC_URL", DEFAULT_PUBLIC_URL),
            web_origins=(
                tuple(os.environ["ASR_WEB_ORIGINS"].split(","))
                if os.environ.get("ASR_WEB_ORIGINS")
                else ()
            ),
            extension_urls=(
                tuple(os.environ["ASR_EXTENSION_URLS"].split(","))
                if os.environ.get("ASR_EXTENSION_URLS")
                else ()
            ),
            chatgpt_callback_url=os.environ.get("ASR_CHATGPT_CALLBACK_URL") or None,
            cookie_secure=_bool_from_env("ASR_COOKIE_SECURE", False),
            max_upload_bytes=_positive_int_from_env(
                "ASR_MAX_UPLOAD_BYTES", DEFAULT_MAX_UPLOAD_BYTES
            ),
            upload_chunk_bytes=_positive_int_from_env(
                "ASR_UPLOAD_CHUNK_BYTES", DEFAULT_UPLOAD_CHUNK_BYTES
            ),
            max_recording_seconds=_positive_int_from_env(
                "ASR_MAX_RECORDING_SECONDS", DEFAULT_MAX_RECORDING_SECONDS
            ),
            extension_max_staging_bytes=_positive_int_from_env(
                "ASR_EXTENSION_MAX_STAGING_BYTES", EXTENSION_MAX_LOCAL_BYTES
            ),
            extension_max_active_sessions=_positive_int_from_env(
                "ASR_EXTENSION_MAX_ACTIVE_SESSIONS", EXTENSION_MAX_ACTIVE_SESSIONS
            ),
            asr_model_path=(
                Path(os.environ["ASR_MODEL_PATH"]) if os.environ.get("ASR_MODEL_PATH") else None
            ),
            asr_device=os.environ.get("ASR_DEVICE", "auto"),
            asr_compute_type=os.environ.get("ASR_COMPUTE_TYPE") or None,
            asr_gpu_model_path=Path(
                os.environ.get("ASR_GPU_MODEL_PATH", str(DEFAULT_ASR_GPU_MODEL_PATH))
            ),
            asr_cpu_model_path=Path(
                os.environ.get("ASR_CPU_MODEL_PATH", str(DEFAULT_ASR_CPU_MODEL_PATH))
            ),
            asr_phowhisper_model_path=Path(
                os.environ.get("ASR_PHOWHISPER_MODEL_PATH", str(DEFAULT_ASR_PHOWHISPER_MODEL_PATH))
            ),
            asr_qwen_model_path=Path(
                os.environ.get("ASR_QWEN_MODEL_PATH", str(DEFAULT_ASR_QWEN_MODEL_PATH))
            ),
            asr_gpu_compute_type=os.environ.get("ASR_GPU_COMPUTE_TYPE", "float16"),
            asr_cpu_compute_type=os.environ.get("ASR_CPU_COMPUTE_TYPE", "int8"),
            diarization_model_path=Path(
                os.environ.get("ASR_DIARIZATION_MODEL_PATH", str(DEFAULT_DIARIZATION_MODEL_PATH))
            ),
        )

    @property
    def origin(self) -> tuple[str, str, int]:
        parsed = urlsplit(self.public_url)
        default_port = 443 if parsed.scheme == "https" else 80
        return parsed.scheme, (parsed.hostname or "").lower(), parsed.port or default_port

    @property
    def host_header(self) -> str:
        parsed = urlsplit(self.public_url)
        return parsed.netloc

    @property
    def extension_server_urls(self) -> tuple[str, ...]:
        return (_normalized_extension_url(self.public_url), *self.extension_urls)

    @property
    def web_server_origins(self) -> tuple[str, ...]:
        return (self.public_url, *self.web_origins)

    def web_origin_for_host(self, host_header: str | None) -> str | None:
        matches = [
            origin
            for origin in self.web_server_origins
            if _host_matches_origin(host_header, origin)
        ]
        return matches[0] if len(matches) == 1 else None

    def select_web_origin(self, requested_origin: str | None) -> str:
        if requested_origin is None:
            return self.public_url
        normalized = _normalized_web_url(requested_origin)
        if normalized not in self.web_server_origins:
            raise ValueError("The requested GUI origin is not configured")
        return normalized

    def callback_host_matches(self, host_header: str | None) -> bool:
        return bool(
            self.chatgpt_callback_url
            and _host_matches_origin(host_header, self.chatgpt_callback_url)
        )

    def extension_endpoint_for_host(self, host_header: str | None) -> str | None:
        if _host_header_parts(host_header) is None:
            return None
        matches: list[str] = []
        for endpoint in self.extension_server_urls:
            if _host_matches_origin(host_header, endpoint):
                matches.append(endpoint)
        return matches[0] if len(matches) == 1 else None

    @property
    def access_url_prefix(self) -> str:
        return f"{self.public_url}/access#"
