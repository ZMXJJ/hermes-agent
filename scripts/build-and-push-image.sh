#!/usr/bin/env bash
# Build a Hermes Agent Docker image and push to Alibaba Cloud ACR.
#
# Usage:
#   ./scripts/build-and-push-image.sh              # default build01
#   ./scripts/build-and-push-image.sh build02      # specify build number
#
# Environment variables:
#   DOCKER_BUILD_PROXY  — proxy for build context (default: http://host.docker.internal:10808)
#   DOCKER_PUSH_PROXY   — proxy for push (default: http://127.0.0.1:10808)
#   Set to empty string to disable proxy.
#
# See docs/docker-image-build-guide.md for full documentation.
set -euo pipefail

cd "$(dirname "$0")/.."

REGISTRY="modelbest-registry.cn-beijing.cr.aliyuncs.com/openbmb/hermes-agent"
VERSION=$(grep '^version' pyproject.toml | sed 's/.*"\(.*\)"/\1/')
DATE=$(date +%Y%m%d)
BUILD_NUM="${1:-build01}"
TAG="${VERSION}-supercpm-${DATE}-${BUILD_NUM}"

PROXY="${DOCKER_BUILD_PROXY:-http://host.docker.internal:10808}"
PUSH_PROXY="${DOCKER_PUSH_PROXY:-http://127.0.0.1:10808}"

echo "=== Hermes Agent Docker Image Builder ==="
echo "Version:  ${VERSION}"
echo "Tag:      ${TAG}"
echo "Registry: ${REGISTRY}"
echo ""

# --- Build ---
echo "=== Building ${REGISTRY}:${TAG} ==="

PROXY_ARGS=()
if [ -n "$PROXY" ]; then
    PROXY_ARGS=(
        --build-arg "HTTP_PROXY=${PROXY}"
        --build-arg "HTTPS_PROXY=${PROXY}"
        --build-arg "NO_PROXY=localhost,127.0.0.1"
    )
    echo "Build proxy: ${PROXY}"
fi

docker buildx build \
    --platform linux/arm64 \
    "${PROXY_ARGS[@]}" \
    -t "${REGISTRY}:${TAG}" \
    -t "${REGISTRY}:latest" \
    --load \
    .

echo ""
echo "=== Build complete ==="
docker images --format "table {{.Repository}}:{{.Tag}}\t{{.Size}}" | grep hermes-agent | head -5
echo ""

# --- Push ---
echo "=== Pushing ${TAG} ==="
if [ -n "$PUSH_PROXY" ]; then
    HTTPS_PROXY="${PUSH_PROXY}" docker push "${REGISTRY}:${TAG}"
else
    docker push "${REGISTRY}:${TAG}"
fi

echo ""
echo "=== Pushing latest ==="
if [ -n "$PUSH_PROXY" ]; then
    HTTPS_PROXY="${PUSH_PROXY}" docker push "${REGISTRY}:latest"
else
    docker push "${REGISTRY}:latest"
fi

echo ""
echo "=== Done ==="
echo "Image: ${REGISTRY}:${TAG}"
echo "Also:  ${REGISTRY}:latest"
docker inspect --format='Digest: {{index .RepoDigests 0}}' "${REGISTRY}:${TAG}" 2>/dev/null || true
