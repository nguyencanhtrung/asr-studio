#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$script_dir"

usage() {
    cat <<'HELP'
Usage: ./scripts/start.sh [--source | --pull | --offline]

Start the app and worker with one resolved Compose image.

  --source   Build ASR_IMAGE from this checkout, then prepare local models.
  --pull     Pull the versioned/digest ASR_IMAGE, verify it, then prepare models.
  --offline  Use an existing local ASR_IMAGE and prepared models only.
  -h, --help Show this help.

With no mode, --source is used. Set ASR_IMAGE in .env to select the image.
Pull mode requires a registry reference with a version tag or sha256 digest.
Every mode stops when the image revision label differs from this checkout's commit.
HELP
}

fail() {
    printf '%s\n' "$1" >&2
    exit "${2:-1}"
}

mode=source
mode_selected=0
while [ "$#" -gt 0 ]; do
    case "$1" in
        --source|--pull|--offline)
            if [ "$mode_selected" -eq 1 ]; then
                fail "Choose only one startup mode." 2
            fi
            mode=${1#--}
            mode_selected=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            usage >&2
            fail "Unknown startup option: $1" 2
            ;;
    esac
done

# The model installer writes /models, a host directory, as the user that owns it.
ASR_MODEL_UID=${ASR_MODEL_UID:-$(id -u)}
ASR_MODEL_GID=${ASR_MODEL_GID:-$(id -g)}
export ASR_MODEL_UID ASR_MODEL_GID

compose_base() {
    docker compose -f compose.yaml "$@"
}

gpu_compose_file=""
compose_runtime() {
    if [ -n "$gpu_compose_file" ]; then
        docker compose -f compose.yaml -f "$gpu_compose_file" "$@"
    else
        compose_base "$@"
    fi
}

compose_environment=$(compose_base config --environment)
compose_images=$(compose_base config --images)
selected_image=$(printf '%s\n' "$compose_images" | sed -n '1p')
distinct_images=$(printf '%s\n' "$compose_images" | sort -u | grep -c . || true)
if [ -z "$selected_image" ] || [ "$distinct_images" -ne 1 ]; then
    fail "Compose app, worker and model-installer must resolve to the same single ASR_IMAGE."
fi

source_version=$(sed -n 's/^__version__ = "\([^"]*\)"$/\1/p' src/asr_studio/__init__.py)
source_schema=$(sed -n 's/^SCHEMA_VERSION = \([0-9][0-9]*\)$/\1/p' src/asr_studio/storage.py)
if [ -z "$source_version" ] || [ -z "$source_schema" ]; then
    fail "Could not read source application version and supported schema."
fi

if [ -z "${ASR_SOURCE_REVISION:-}" ]; then
    # Only a repository rooted in this directory names this checkout, not an enclosing one.
    git_toplevel=$(git rev-parse --show-toplevel 2>/dev/null || true)
    if [ -n "$git_toplevel" ] && [ "$git_toplevel" = "$(pwd -P)" ]; then
        ASR_SOURCE_REVISION=$(git rev-parse --verify HEAD 2>/dev/null || true)
    fi
    ASR_SOURCE_REVISION=${ASR_SOURCE_REVISION:-unknown}
    export ASR_SOURCE_REVISION
fi

requested_device=$(printf '%s\n' "$compose_environment" | sed -n 's/^ASR_DEVICE=//p')
requested_device=${requested_device:-auto}
case "$requested_device" in
    auto|cpu|cuda) ;;
    *) fail "ASR_DEVICE must be auto, cpu, or cuda." 2 ;;
esac

model_host_path=$(printf '%s\n' "$compose_environment" | sed -n 's/^ASR_MODEL_HOST_PATH=//p')
model_host_path=${model_host_path:-./models}
case "$model_host_path" in
    /*) ;;
    *) model_host_path="$script_dir/$model_host_path" ;;
esac
if [ "$mode" = offline ]; then
    if [ ! -d "$model_host_path" ]; then
        fail "startup_model_unavailable: offline mode requires prepared local models."
    fi
else
    mkdir -p "$model_host_path"
fi
model_host_path=$(CDPATH= cd -- "$model_host_path" && pwd -P)

diarization_container_path=$(printf '%s\n' "$compose_environment" | \
    sed -n 's/^ASR_DIARIZATION_MODEL_PATH=//p')
diarization_container_path=${diarization_container_path:-/models/speaker-diarization-community-1}
case "$diarization_container_path" in
    /models/*)
        diarization_relative_path=${diarization_container_path#/models/}
        ;;
    *)
        fail "ASR_DIARIZATION_MODEL_PATH must be below /models inside the container."
        ;;
esac
case "$diarization_relative_path" in
    ""|..|../*|*/..|*/../*)
        fail "ASR_DIARIZATION_MODEL_PATH must not escape /models."
        ;;
esac
diarization_model_path="$model_host_path/$diarization_relative_path"
print_community_1_download_guidance() {
    echo "Before downloading, accept the Community-1 conditions at:" >&2
    echo "  https://huggingface.co/pyannote/speaker-diarization-community-1" >&2
    echo "Authenticate on this host with: hf auth login --add-to-git-credential" >&2
    echo "Run git lfs install, then clone Community-1 into the configured model directory." >&2
    echo "Move any incomplete copy out of the way before cloning." >&2
}

for required_model_file in \
    config.yaml \
    embedding/pytorch_model.bin \
    plda/plda.npz \
    plda/xvec_transform.npz \
    segmentation/pytorch_model.bin
do
    required_model_path="$diarization_model_path/$required_model_file"
    if [ ! -f "$required_model_path" ]; then
        echo "startup_model_incomplete: stage=startup_model_validation" >&2
        print_community_1_download_guidance
        exit 1
    fi
    if [ ! -s "$required_model_path" ]; then
        echo "startup_model_incomplete: stage=startup_model_validation" >&2
        print_community_1_download_guidance
        exit 1
    fi
    lfs_pointer_prefix='version https://git-lfs.github.com/spec/v1'
    if dd if="$required_model_path" bs="${#lfs_pointer_prefix}" count=1 2>/dev/null | \
        LC_ALL=C grep -F -x "$lfs_pointer_prefix" >/dev/null 2>&1; then
        echo "startup_model_incomplete: stage=startup_model_validation" >&2
        print_community_1_download_guidance
        exit 1
    fi
done

validate_pull_image() {
    image_repository=${selected_image%@*}
    case "$selected_image" in
        *@sha256:*)
            image_digest=${selected_image##*@sha256:}
            case "$image_repository" in
                */*) ;;
                *) fail "Pull mode requires a registry image reference, not a local image name." 2 ;;
            esac
            case "$image_digest" in
                ''|*[!0123456789abcdef]*)
                    fail "ASR_IMAGE must use a lowercase sha256 digest." 2
                    ;;
            esac
            if [ "${#image_digest}" -ne 64 ]; then
                fail "ASR_IMAGE sha256 digest must contain 64 hexadecimal characters." 2
            fi
            ;;
        *)
            case "$image_repository" in
                */*) ;;
                *) fail "Pull mode requires a registry image reference, not a local image name." 2 ;;
            esac
            image_leaf=${selected_image##*/}
            case "$image_leaf" in
                *:*) image_tag=${image_leaf##*:} ;;
                *) fail "Pull mode requires ASR_IMAGE to include an explicit version tag or sha256 digest." 2 ;;
            esac
            case "$image_tag" in
                local|latest|edge|'')
                    fail "Pull mode does not accept local, latest, or edge image tags." 2
                    ;;
            esac
            if ! printf '%s\n' "$image_tag" | grep -Eq '^v?[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$'; then
                fail "Pull mode requires an explicit version tag (for example v0.1.0-rc.1) or sha256 digest." 2
            fi
            ;;
    esac
}

check_image_revision() {
    image_revision=$(docker image inspect \
        --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' \
        "$selected_image" 2>/dev/null || true)
    case "$image_revision" in
        ''|unknown|'<no value>') image_revision=unknown ;;
    esac
    if [ "$ASR_SOURCE_REVISION" = unknown ] || [ "$image_revision" = unknown ]; then
        echo "Cannot verify that $selected_image was built from this checkout." >&2
        echo "Checkout revision: $ASR_SOURCE_REVISION; image revision: $image_revision." >&2
        echo "Use a git checkout of the release tag and an image built from it to verify parity." >&2
    elif [ "$image_revision" != "$ASR_SOURCE_REVISION" ]; then
        fail "Image $selected_image was built from revision $image_revision, but this checkout is $ASR_SOURCE_REVISION. Check out the matching tag or select the matching image."
    fi
}

case "$mode" in
    source)
        compose_base build
        ;;
    pull)
        validate_pull_image
        docker pull "$selected_image"
        ;;
    offline)
        if ! docker image inspect "$selected_image" >/dev/null 2>&1; then
            fail "Offline mode requires the image already present locally: $selected_image"
        fi
        ;;
esac

if ! docker run --pull=never --rm --network none --entrypoint python "$selected_image" \
    -m asr_studio.cli version --json --expect-version "$source_version" \
    --expect-schema "$source_schema"; then
    fail "Image $selected_image does not match source version $source_version and schema $source_schema."
fi
check_image_revision

if [ "$requested_device" = cpu ]; then
    echo "CPU backend requested; selecting whisper-small."
elif docker run --pull=never --rm --gpus all --network none --entrypoint python "$selected_image" \
    -c 'import ctranslate2; raise SystemExit(0 if ctranslate2.get_cuda_device_count() else 1)' \
    >/dev/null 2>&1; then
    gpu_compose_file="compose.gpu.yaml"
    echo "Detected NVIDIA GPU; selecting CUDA and whisper-large-v3."
elif [ "$requested_device" = cuda ] || \
    { command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L >/dev/null 2>&1; }; then
    echo "NVIDIA GPU requested or detected, but Docker cannot expose it." >&2
    echo "Check the NVIDIA driver and NVIDIA Container Toolkit." >&2
    exit 1
else
    echo "No NVIDIA GPU detected; selecting CPU and whisper-small."
fi

if [ "$mode" = offline ]; then
    compose_runtime run --pull never --rm --no-deps worker python -m asr_studio.cli model check
else
    compose_runtime -f compose.model-prepare.yaml run --pull never --rm --no-deps \
        --user "$(id -u):$(id -g)" \
        -e HF_HOME=/tmp/huggingface -e HF_HUB_OFFLINE=0 -e TRANSFORMERS_OFFLINE=0 \
        worker python -m asr_studio.cli model prepare
fi
compose_runtime run --pull never --rm --no-deps worker python -m asr_studio.cli runtime check
# Offline means no download: the one service that can make one stays stopped, even for a queued job.
installer_scale=""
if [ "$mode" = offline ]; then
    installer_scale="--scale model-installer=0"
fi
# shellcheck disable=SC2086
compose_runtime up --pull never --no-build --detach --wait $installer_scale \
    --wait-timeout "${ASR_STARTUP_TIMEOUT:-120}"
compose_runtime exec -T app python -m asr_studio.cli installation check
if [ -t 1 ]; then
    compose_runtime exec -T app python -m asr_studio.cli access show
else
    printf '%s\n' \
        "Startup complete; access details are withheld from non-interactive output." \
        "Run 'docker compose exec -T app python -m asr_studio.cli access show' to display the access link and QR."
fi
