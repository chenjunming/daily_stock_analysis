#!/usr/bin/env bash
set -euo pipefail
# bash scripts/build_and_push_arm64.sh --push --builder default

IMAGE=""
TAG=""
DOCKERFILE="docker/Dockerfile"
CONTEXT="."
PLATFORM="linux/arm64"
BUILDER="local-multiarch"
CACHE_ENABLED=true
CACHE_DIR=".buildx-cache"
CACHE_FROM=""
CACHE_TO=""
PIP_INDEX_URL=""
PIP_EXTRA_INDEX_URL=""
PIP_TRUSTED_HOST=""
QUICK_PUSH=false
QUICK_PULL=false
QUICK_PULL_ARM64=false
QUICK_IMAGE="docker.io/chenjunming123/stock-analyzer"
QUICK_TAG="latest"

usage() {
  cat <<'EOF'
Build and push an arm64 Docker image with buildx.

Usage:
  scripts/build_and_push_arm64.sh --image <repo/image> [options]
  scripts/build_and_push_arm64.sh --push [options]
  scripts/build_and_push_arm64.sh --pull [options]
  scripts/build_and_push_arm64.sh --pull-arm64 [options]

Required:
  --image         Target image repository, e.g. ghcr.io/owner/repo or docker.io/user/repo (optional when using --push)

Optional:
  --push          Quick push preset image/tag: docker.io/chenjunming123/stock-analyzer:latest
  --pull          Quick pull preset image/tag: docker.io/chenjunming123/stock-analyzer:latest
  --pull-arm64    Quick pull preset image/tag with --platform linux/arm64
  --tag           Image tag (default: current datetime, e.g. 20260207-171500)
  --dockerfile    Dockerfile path (default: docker/Dockerfile)
  --context       Build context path (default: .)
  --platform      Target platform (default: linux/arm64)
  --builder       buildx builder name (default: local-multiarch)
  --cache-dir     Local buildx cache directory (default: .buildx-cache)
  --cache-from    buildx cache-from value, e.g. type=registry,ref=repo/image:buildcache
  --cache-to      buildx cache-to value, e.g. type=registry,ref=repo/image:buildcache,mode=max
  --no-cache-use  Disable buildx cache import/export
  --pip-index-url       Pip index URL for faster dependency download
  --pip-extra-index-url Extra pip index URL
  --pip-trusted-host    Trusted host for pip index (e.g. pypi.tuna.tsinghua.edu.cn)
  -h, --help      Show this help

Examples:
  scripts/build_and_push_arm64.sh --image ghcr.io/acme/daily-stock-analysis --tag latest
  scripts/build_and_push_arm64.sh --image docker.io/myuser/stock-analyzer --tag v1.0.0

Quick Commands (your repo):
  Push: bash scripts/build_and_push_arm64.sh --image docker.io/chenjunming123/stock-analyzer --tag latest
  Pull: docker pull docker.io/chenjunming123/stock-analyzer:latest
  Pull arm64 explicitly: docker pull --platform linux/arm64 docker.io/chenjunming123/stock-analyzer:latest
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --image)
      IMAGE="${2:-}"
      shift 2
      ;;
    --push)
      QUICK_PUSH=true
      shift
      ;;
    --pull)
      QUICK_PULL=true
      shift
      ;;
    --pull-arm64)
      QUICK_PULL_ARM64=true
      shift
      ;;
    --tag)
      TAG="${2:-}"
      shift 2
      ;;
    --dockerfile)
      DOCKERFILE="${2:-}"
      shift 2
      ;;
    --context)
      CONTEXT="${2:-}"
      shift 2
      ;;
    --platform)
      PLATFORM="${2:-}"
      shift 2
      ;;
    --builder)
      BUILDER="${2:-}"
      shift 2
      ;;
    --cache-dir)
      CACHE_DIR="${2:-}"
      shift 2
      ;;
    --cache-from)
      CACHE_FROM="${2:-}"
      shift 2
      ;;
    --cache-to)
      CACHE_TO="${2:-}"
      shift 2
      ;;
    --no-cache-use)
      CACHE_ENABLED=false
      shift
      ;;
    --pip-index-url)
      PIP_INDEX_URL="${2:-}"
      shift 2
      ;;
    --pip-extra-index-url)
      PIP_EXTRA_INDEX_URL="${2:-}"
      shift 2
      ;;
    --pip-trusted-host)
      PIP_TRUSTED_HOST="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 1
      ;;
  esac
done

if [[ -z "$IMAGE" ]]; then
  if [[ "$QUICK_PUSH" == "true" || "$QUICK_PULL" == "true" || "$QUICK_PULL_ARM64" == "true" ]]; then
    IMAGE="$QUICK_IMAGE"
    if [[ -z "$TAG" ]]; then
      TAG="$QUICK_TAG"
    fi
  else
    echo "Error: --image is required." >&2
    usage
    exit 1
  fi
fi

if [[ -z "$TAG" ]]; then
  TAG="$(date +%Y%m%d-%H%M%S)"
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "Error: docker is not installed." >&2
  exit 1
fi

if [[ "$QUICK_PULL" == "true" || "$QUICK_PULL_ARM64" == "true" ]]; then
  if [[ -z "$TAG" ]]; then
    TAG="$QUICK_TAG"
  fi
  IMAGE_REF="${IMAGE}:${TAG}"
  if [[ "$QUICK_PULL_ARM64" == "true" ]]; then
    echo "Pulling arm64 image: $IMAGE_REF"
    docker pull --platform linux/arm64 "$IMAGE_REF"
  else
    echo "Pulling image: $IMAGE_REF"
    docker pull "$IMAGE_REF"
  fi
  echo "Done: $IMAGE_REF"
  exit 0
fi

if ! docker buildx version >/dev/null 2>&1; then
  echo "Error: docker buildx is not available." >&2
  exit 1
fi

if [[ ! -f "$DOCKERFILE" ]]; then
  echo "Error: Dockerfile not found: $DOCKERFILE" >&2
  exit 1
fi

if [[ ! -d "$CONTEXT" ]]; then
  echo "Error: build context directory not found: $CONTEXT" >&2
  exit 1
fi

ensure_builder() {
  if ! docker buildx inspect "$BUILDER" >/dev/null 2>&1; then
    echo "Creating buildx builder: $BUILDER"
    docker buildx create --name "$BUILDER" --driver docker-container >/dev/null
  fi

  docker buildx use "$BUILDER"
  if ! docker buildx inspect "$BUILDER" --bootstrap >/dev/null 2>&1; then
    echo "Warning: builder bootstrap failed, recreating: $BUILDER"
    docker rm -f "buildx_buildkit_${BUILDER}0" >/dev/null 2>&1 || true
    docker buildx rm "$BUILDER" >/dev/null 2>&1 || true
    docker buildx create --name "$BUILDER" --driver docker-container >/dev/null
    docker buildx use "$BUILDER"
    docker buildx inspect "$BUILDER" --bootstrap >/dev/null
  fi
}

ensure_builder

if ! docker buildx inspect "$BUILDER" | grep -q "$PLATFORM"; then
  echo "Installing binfmt for arm64 emulation..."
  docker run --privileged --rm tonistiigi/binfmt --install arm64 >/dev/null
  # buildkit container needs restart to pick up new binfmt handlers.
  echo "Recreating builder to refresh supported platforms..."
  docker rm -f "buildx_buildkit_${BUILDER}0" >/dev/null 2>&1 || true
  docker buildx rm "$BUILDER" >/dev/null 2>&1 || true
  docker buildx create --name "$BUILDER" --driver docker-container >/dev/null
  ensure_builder
fi

if ! docker buildx inspect "$BUILDER" | grep -q "$PLATFORM"; then
  echo "Error: buildx builder does not support $PLATFORM." >&2
  exit 1
fi

INSPECT_TEXT="$(docker buildx inspect "$BUILDER")"
BUILD_DRIVER="$(printf '%s\n' "$INSPECT_TEXT" | awk -F': *' '/^Driver:/{print $2; exit}')"

IMAGE_REF="${IMAGE}:${TAG}"
BUILD_ARGS=(
  --platform "$PLATFORM"
  -f "$DOCKERFILE"
  -t "$IMAGE_REF"
  --push
)

if [[ -n "$PIP_INDEX_URL" ]]; then
  BUILD_ARGS+=(--build-arg "PIP_INDEX_URL=$PIP_INDEX_URL")
fi
if [[ -n "$PIP_EXTRA_INDEX_URL" ]]; then
  BUILD_ARGS+=(--build-arg "PIP_EXTRA_INDEX_URL=$PIP_EXTRA_INDEX_URL")
fi
if [[ -n "$PIP_TRUSTED_HOST" ]]; then
  BUILD_ARGS+=(--build-arg "PIP_TRUSTED_HOST=$PIP_TRUSTED_HOST")
fi

if [[ "$CACHE_ENABLED" == "true" ]]; then
  CACHE_BACKEND_SUPPORTED=true
  # docker driver 默认不支持 cache export（除非开启 containerd image store）
  # 避免直接报错中断，自动降级为不传 cache 参数。
  if [[ "$BUILD_DRIVER" == "docker" ]]; then
    echo "Warning: builder '$BUILDER' uses docker driver; skipping --cache-from/--cache-to." >&2
    echo "Hint: use --builder local-multiarch (docker-container) for full cache import/export." >&2
    CACHE_BACKEND_SUPPORTED=false
    CACHE_FROM=""
    CACHE_TO=""
  fi

  if [[ "$CACHE_BACKEND_SUPPORTED" == "true" ]]; then
    if [[ -z "$CACHE_TO" ]]; then
      CACHE_TO="type=local,dest=${CACHE_DIR},mode=max"
    fi

    if [[ -z "$CACHE_FROM" ]]; then
      mkdir -p "$CACHE_DIR"
      if [[ -f "${CACHE_DIR}/index.json" ]]; then
        CACHE_FROM="type=local,src=${CACHE_DIR}"
      fi
    fi

    if [[ -n "$CACHE_FROM" ]]; then
      BUILD_ARGS+=(--cache-from "$CACHE_FROM")
    fi
    if [[ -n "$CACHE_TO" ]]; then
      BUILD_ARGS+=(--cache-to "$CACHE_TO")
    fi
  fi
fi

echo "Building and pushing image: $IMAGE_REF"
echo "Platform: $PLATFORM"
echo "Dockerfile: $DOCKERFILE"
echo "Context: $CONTEXT"
if [[ "$CACHE_ENABLED" == "true" ]]; then
  echo "Cache: enabled"
  echo "Cache-from: ${CACHE_FROM:-<none(first build)>}"
  echo "Cache-to: ${CACHE_TO:-<none>}"
else
  echo "Cache: disabled"
fi
if [[ -n "$PIP_INDEX_URL" ]]; then
  echo "Pip index: $PIP_INDEX_URL"
fi
if [[ -n "$PIP_EXTRA_INDEX_URL" ]]; then
  echo "Pip extra index: $PIP_EXTRA_INDEX_URL"
fi
echo
echo "Tip: ensure you have logged in to registry first (docker login ...)."
echo

docker buildx build --builder "$BUILDER" "${BUILD_ARGS[@]}" "$CONTEXT"

echo
echo "Done: $IMAGE_REF"
