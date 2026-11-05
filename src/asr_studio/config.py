"""Runtime configuration with strict localhost URL validation."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_PUBLIC_URL = "http://127.0.0.1:8765"
DEFAULT_PROVIDER_DIR = Path("/var/lib/asr-studio/provider")
DEFAULT_INSTALL_DIR = Path("/var/lib/asr-studio/install")
CHATGPT_CALLBACK_PATH = "/auth/chatgpt/callback"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost"})
DEFAULT_MAX_UPLOAD_BYTES = 4 * 1024**3
DEFAULT_MAX_RECORDING_SECONDS = 4 * 60 * 60
DEFAULT_UPLOAD_CHUNK_BYTES = 64 * 1024**2
DEFAULT_ASR_GPU_MODEL_PATH = Path("/models/whisper-large-v3")
DEFAULT_ASR_CPU_MODEL_PATH = Path("/models/whisper-small")
DEFAULT_ASR_PHOWHISPER_MODEL_PATH = Path("/models/pho-whisper-large")
DEFAULT_ASR_QWEN_MODEL_PATH = Path("/models/qwen3-asr-1.7b")
DEFAULT_DIARIZATION_MODEL_PATH = Path("/models/speaker-diarization-community-1")
ASR_DEVICES = frozenset({"auto", "cpu", "cuda"})
ASR_COMPUTE_TYPES = frozenset(
    {"default", "float16", "float32", "int8", "int8_float16", "int8_float32"}
)


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
    parsed = urlsplit(raw_url)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname is None
        or parsed.hostname.lower() not in LOOPBACK_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("ASR_PUBLIC_URL must be an http(s) localhost origin without a path")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("ASR_PUBLIC_URL has an invalid port") from exc
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("ASR_PUBLIC_URL has an invalid port")
    return f"{parsed.scheme}://{parsed.netloc}"


def _validate_chatgpt_callback_url(raw_url: str) -> str:
    parsed = urlsplit(raw_url)
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
    def access_url_prefix(self) -> str:
        return f"{self.public_url}/access#"
