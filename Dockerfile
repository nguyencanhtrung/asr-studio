FROM ghcr.io/astral-sh/uv:0.11.15 AS uv-bin

FROM nvidia/cuda:12.8.1-cudnn-runtime-ubuntu24.04 AS python-base
ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        ffmpeg \
        libgomp1 \
        libpython3.12 \
        python3 \
        python3-venv \
    && rm -rf /var/lib/apt/lists/*

RUN if [ "$(dpkg --print-architecture)" = amd64 ]; then \
        apt-get update && apt-get install -y --no-install-recommends libquadmath0 \
        && rm -rf /var/lib/apt/lists/*; fi

FROM python-base AS media-builder
RUN apt-get update && apt-get install -y --no-install-recommends build-essential curl pkg-config xz-utils \
    && rm -rf /var/lib/apt/lists/*
COPY scripts/build-lgpl-media.sh /tmp/build-lgpl-media.sh
RUN sh /tmp/build-lgpl-media.sh

FROM python-base AS builder
RUN apt-get update && apt-get install -y --no-install-recommends build-essential binutils pkg-config python3-dev \
    && rm -rf /var/lib/apt/lists/*
COPY --from=media-builder /opt/asr-media /opt/asr-media
COPY --from=uv-bin /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_PYTHON=/usr/bin/python3
WORKDIR /app
COPY pyproject.toml README.md LICENSE uv.lock ./
COPY src/ ./src/
COPY scripts/use-system-openmp.py /tmp/use-system-openmp.py
RUN PKG_CONFIG_PATH=/opt/asr-media/lib/pkgconfig LDFLAGS=-Wl,-rpath,/opt/asr-media/lib \
    uv sync --locked --no-dev --no-editable --no-binary-package av \
    && /app/.venv/bin/python /tmp/use-system-openmp.py \
    && /app/.venv/bin/python -c "import ctranslate2; assert ctranslate2.get_supported_compute_types('cpu')"

FROM python-base AS omp-bin
ARG TARGETARCH=amd64
ARG OMP_VERSION=18.5.0
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && case "${TARGETARCH}" in \
        amd64) omp_asset=omp-linux-x64; omp_sha=b8413e6e085a423c7a9b1d7bc025c2af794923b6b6c57330a309639b6176c8a1 ;; \
        arm64) omp_asset=omp-linux-arm64; omp_sha=1244860c58bb68a3a45390d4932e5c7983183ec8c12cb6848c1afd709de38a07 ;; \
        *) echo "Unsupported OMP architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac \
    && curl --fail --location --proto '=https' --tlsv1.2 \
        "https://github.com/can1357/oh-my-pi/releases/download/v${OMP_VERSION}/${omp_asset}" \
        --output /usr/local/bin/omp \
    && echo "${omp_sha}  /usr/local/bin/omp" | sha256sum --check --strict \
    && chmod 0755 /usr/local/bin/omp \
    && mkdir -p /usr/local/share/licenses/omp \
    && curl --fail --location --proto '=https' --tlsv1.2 \
        "https://github.com/can1357/oh-my-pi/releases/download/v${OMP_VERSION}/LICENSE" \
        --output /usr/local/share/licenses/omp/LICENSE \
    && curl --fail --location --proto '=https' --tlsv1.2 \
        "https://github.com/can1357/oh-my-pi/releases/download/v${OMP_VERSION}/THIRD-PARTY-NOTICES.txt" \
        --output /usr/local/share/licenses/omp/THIRD-PARTY-NOTICES.txt

FROM python-base AS runtime
ARG VCS_REF=unknown
ENV PATH="/app/.venv/bin:${PATH}" \
    LD_LIBRARY_PATH="/opt/asr-media/lib:${LD_LIBRARY_PATH}" \
    MPLCONFIGDIR=/tmp/matplotlib \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    XDG_CACHE_HOME=/tmp/cache \
    DO_NOT_TRACK=1 \
    HF_HUB_DISABLE_TELEMETRY=1 \
    HF_HUB_DISABLE_PROGRESS_BARS=1 \
    PYANNOTE_METRICS_ENABLED=0 \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    ASR_DATA_DIR=/var/lib/asr-studio/data \
    ASR_AUTH_DIR=/var/lib/asr-studio/auth
LABEL org.opencontainers.image.revision="${VCS_REF}"
LABEL org.opencontainers.image.licenses="PolyForm-Noncommercial-1.0.0"
WORKDIR /app
COPY LICENSE /usr/local/share/licenses/asr-studio/
COPY third_party/notices/ /usr/local/share/licenses/asr-studio/third-party/
COPY third_party/platform-coverage.json /usr/local/share/licenses/asr-studio/platform-coverage.json
COPY --from=builder /app/.venv /app/.venv
COPY --from=media-builder /opt/asr-media/lib /opt/asr-media/lib
COPY --from=media-builder /opt/asr-media/share/licenses /usr/local/share/licenses/asr-studio/media/
COPY --from=builder /app/openmp-runtime.json /usr/local/share/licenses/asr-studio/openmp-runtime.json
COPY --from=omp-bin /usr/local/bin/omp /usr/local/bin/omp
COPY --from=omp-bin /usr/local/share/licenses/omp /usr/local/share/licenses/omp
RUN groupadd --gid 10001 asr \
    && useradd --uid 10001 --gid asr --no-create-home --shell /usr/sbin/nologin asr \
    && mkdir -p /var/lib/asr-studio/data /var/lib/asr-studio/auth /var/lib/asr-studio/provider \
    && chown -R 10001:10001 /var/lib/asr-studio \
    && chmod 0700 /var/lib/asr-studio/data /var/lib/asr-studio/auth /var/lib/asr-studio/provider \
    && chmod -R a+rX /usr/local/share/licenses
USER 10001:10001
CMD ["uvicorn", "asr_studio.app:app", "--host", "0.0.0.0", "--port", "8765", "--no-proxy-headers", "--no-server-header", "--no-access-log"]
