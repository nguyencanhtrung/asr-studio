"""Runtime configuration with strict GUI-origin and callback URL validation."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_PUBLIC_URL = "http://127.0.0.1:8765"
DEFAULT_PROVIDER_DIR = Path("/var/lib/asr-studio/provider")
DEFAULT_INSTALL_DIR = Path("/var/lib/asr-studio/install")
CHATGPT_CALLBACK_PATH = "/auth/chatgpt/callback"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
MAX_EXTENSION_URLS = 8
MAX_EXTENSION_URL_BYTES = 256
DEFAULT_MAX_UPLOAD_BYTES = 4 * 1024**3
DEFAULT_MAX_RECORDING_SECONDS = 4 * 60 * 60
DEFAULT_UPLOAD_CHUNK_BYTES = 64 * 1024**2
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


def _normalized_extension_url(raw_url: str) -> str:
    if (
        not isinstance(raw_url, str)
        or not raw_url
        or len(raw_url.encode("utf-8")) > MAX_EXTENSION_URL_BYTES
        or _url_has_unsafe_text(raw_url)
    ):
        raise ValueError("ASR_PUBLIC_URL and ASR_EXTENSION_URLS must be strict server origins")
    try:
        parsed = urlsplit(raw_url)
        hostname = parsed.hostname
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "ASR_PUBLIC_URL and ASR_EXTENSION_URLS must be strict server origins"
        ) from exc
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or "*" in parsed.netloc
    ):
        raise ValueError("ASR_PUBLIC_URL and ASR_EXTENSION_URLS must be strict server origins")
    normalized_hostname = hostname.lower()
    if (
        normalized_hostname.endswith(".")
        or normalized_hostname.startswith(".")
        or ".." in normalized_hostname
        or any(character.isspace() for character in normalized_hostname)
    ):
        raise ValueError("ASR_PUBLIC_URL and ASR_EXTENSION_URLS must be strict server origins")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("ASR extension origin has an invalid port") from exc
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("ASR extension origin has an invalid port")
    loopback = normalized_hostname in LOOPBACK_HOSTS
    scheme = parsed.scheme.lower()
    if scheme == "http" and not loopback:
        raise ValueError("ASR extension origins must use HTTPS except for loopback")
    authority = f"[{normalized_hostname}]" if ":" in normalized_hostname else normalized_hostname
    default_port = 443 if scheme == "https" else 80
    if port is not None and port != default_port:
        authority = f"{authority}:{port}"
    return f"{scheme}://{authority}"


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
    if (
        normalized_hostname.endswith(".")
        or normalized_hostname.startswith(".")
        or ".." in normalized_hostname
    ):
        return None
    return normalized_hostname, port


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


def _validate_public_url(raw_url: str) -> str:
    if not isinstance(raw_url, str) or any(
        ord(character) < 0x20 or ord(character) == 0x7F for character in raw_url
    ):
        raise ValueError(
            "ASR_PUBLIC_URL must be a loopback http(s) origin or an HTTPS public origin"
        )
    try:
        parsed = urlsplit(raw_url)
        hostname = parsed.hostname
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "ASR_PUBLIC_URL must be a loopback http(s) origin or an HTTPS public origin"
        ) from exc
    loopback = hostname is not None and hostname.lower() in LOOPBACK_HOSTS
    if (
        parsed.scheme not in {"http", "https"}
        or hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or "*" in parsed.netloc
        or any(character.isspace() for character in hostname)
        or (not loopback and parsed.scheme != "https")
    ):
        raise ValueError(
            "ASR_PUBLIC_URL must be a loopback http(s) origin or an HTTPS public origin"
        )
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("ASR_PUBLIC_URL has an invalid port") from exc
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("ASR_PUBLIC_URL has an invalid port")
    return f"{parsed.scheme}://{parsed.netloc}"


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
        host_headers = {urlsplit(normalized_default).netloc}
        for alias in normalized_aliases:
            authority = _extension_authority(alias)
            host_header = urlsplit(alias).netloc
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

    def extension_endpoint_for_host(self, host_header: str | None) -> str | None:
        parts = _host_header_parts(host_header)
        if parts is None:
            return None
        hostname, port = parts
        matches: list[str] = []
        for endpoint in self.extension_server_urls:
            expected_hostname, expected_port = _extension_authority(endpoint)
            if hostname != expected_hostname:
                continue
            if port is None:
                parsed = urlsplit(endpoint)
                default_port = 443 if parsed.scheme == "https" else 80
                if expected_port != default_port:
                    continue
            elif port != expected_port:
                continue
            matches.append(endpoint)
        return matches[0] if len(matches) == 1 else None

    @property
    def access_url_prefix(self) -> str:
        return f"{self.public_url}/access#"
