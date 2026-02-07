#!/usr/bin/env bash
set -euo pipefail

IMAGE=""
TAG=""
DOCKERFILE="docker/Dockerfile"
CONTEXT="."
PLATFORM="linux/arm64"
BUILDER="local-multiarch"
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

if ! docker buildx inspect "$BUILDER" >/dev/null 2>&1; then
  echo "Creating buildx builder: $BUILDER"
  docker buildx create --name "$BUILDER" --driver docker-container >/dev/null
fi

docker buildx use "$BUILDER"
docker buildx inspect --bootstrap >/dev/null

if ! docker buildx inspect | grep -q "$PLATFORM"; then
  echo "Installing binfmt for arm64 emulation..."
  docker run --privileged --rm tonistiigi/binfmt --install arm64 >/dev/null
  docker buildx inspect --bootstrap >/dev/null
fi

if ! docker buildx inspect | grep -q "$PLATFORM"; then
  echo "Error: buildx builder does not support $PLATFORM." >&2
  exit 1
fi

IMAGE_REF="${IMAGE}:${TAG}"

echo "Building and pushing image: $IMAGE_REF"
echo "Platform: $PLATFORM"
echo "Dockerfile: $DOCKERFILE"
echo "Context: $CONTEXT"
echo
echo "Tip: ensure you have logged in to registry first (docker login ...)."
echo

docker buildx build \
  --platform "$PLATFORM" \
  -f "$DOCKERFILE" \
  -t "$IMAGE_REF" \
  --push \
  "$CONTEXT"

echo
echo "Done: $IMAGE_REF"
