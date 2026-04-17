#!/usr/bin/env bash
# Cellule.ai release build pipeline
#
# Steps:
#   1. Build the Python wheel (dist/iamine_ai-<version>-py3-none-any.whl)
#   2. Sign the wheel with the maintainer Ed25519 key (produces .whl.sig)
#   3. Verify the signature locally (sanity check)
#   4. Build the Docker image (which copies wheel + sig + MAINTAINERS inside)
#   5. Sign the Docker image digest (produces image-digest.sig)
#   6. Tag with :pinned-<version> and :latest
#
# Does NOT push by default — pass --push to push to Docker Hub.
# :stable is NEVER pushed by this script (requires K>=2 maintainer signatures,
# coordinated manually via the co-maintainers — see docs/GOVERNANCE.md).
#
# Required env (from ../.secret/tokens.env or /etc/iamine/secrets.env):
#   MAINTAINER_SIGNING_KEY_PATH   path to Ed25519 seed (32 bytes)
#   DOCKER_HUB_USER               if --push
#   DOCKER_HUB_TOKEN              if --push
#
# Required args:
#   --signer <nickname>    must exist in MAINTAINERS file
#
# Optional:
#   --push                 push :pinned-<version> and :latest to Docker Hub
#   --image-repo <repo>    default: celluleai/pool
#
# Usage:
#   scripts/build_release.sh --signer molecule
#   scripts/build_release.sh --signer molecule --push

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

PUSH=0
SIGNER=""
IMAGE_REPO="celluleai/pool"

while [ $# -gt 0 ]; do
    case "$1" in
        --push) PUSH=1; shift ;;
        --signer) SIGNER="$2"; shift 2 ;;
        --image-repo) IMAGE_REPO="$2"; shift 2 ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

if [ -z "$SIGNER" ]; then
    echo "FATAL: --signer <nickname> is required (must exist in MAINTAINERS)" >&2
    exit 2
fi

if [ -z "${MAINTAINER_SIGNING_KEY_PATH:-}" ]; then
    echo "FATAL: MAINTAINER_SIGNING_KEY_PATH not set (source your secrets.env first)" >&2
    exit 2
fi

if [ ! -f "$MAINTAINER_SIGNING_KEY_PATH" ]; then
    echo "FATAL: signing key not found at $MAINTAINER_SIGNING_KEY_PATH" >&2
    exit 2
fi

VERSION="$(python -c 'import iamine; print(iamine.__version__)')"
WHEEL_NAME="iamine_ai-${VERSION}-py3-none-any.whl"
WHEEL_PATH="dist/${WHEEL_NAME}"

echo "=========================================="
echo "Cellule.ai release build"
echo "=========================================="
echo "  version:    $VERSION"
echo "  signer:     $SIGNER"
echo "  image:      $IMAGE_REPO:pinned-$VERSION (+ :latest)"
echo "  push:       $PUSH"
echo ""

# Step 1: build wheel
echo ">>> [1/6] Building wheel..."
rm -rf build/ dist/*.whl dist/*.tar.gz 2>/dev/null || true
python -m build --wheel --outdir dist/
if [ ! -f "$WHEEL_PATH" ]; then
    echo "FATAL: expected wheel not produced: $WHEEL_PATH" >&2
    ls -la dist/ >&2
    exit 3
fi
echo "    built: $WHEEL_PATH"

# Step 2: sign wheel
echo ">>> [2/6] Signing wheel..."
python scripts/sign_release.py sign "$WHEEL_PATH" \
    --seed "$MAINTAINER_SIGNING_KEY_PATH" \
    --signer "$SIGNER"

# Step 3: verify sanity
echo ">>> [3/6] Verifying wheel signature..."
python scripts/sign_release.py verify "$WHEEL_PATH" --maintainers MAINTAINERS

# Step 4: build Docker image
echo ">>> [4/6] Building Docker image..."
DOCKER_TAG_PINNED="${IMAGE_REPO}:pinned-${VERSION}"
DOCKER_TAG_LATEST="${IMAGE_REPO}:latest"

cp MAINTAINERS docker/MAINTAINERS
cp -r dist docker/dist
trap 'rm -rf docker/MAINTAINERS docker/dist' EXIT

docker build -f docker/Dockerfile \
    -t "$DOCKER_TAG_PINNED" \
    -t "$DOCKER_TAG_LATEST" \
    docker/

# Step 5: sign image digest
echo ">>> [5/6] Signing Docker image digest..."
IMAGE_DIGEST="$(docker image inspect "$DOCKER_TAG_PINNED" --format='{{index .Id}}' | sed 's/^sha256://')"
echo "    digest: sha256:$IMAGE_DIGEST"
python scripts/sign_release.py sign-digest "$IMAGE_DIGEST" \
    --artifact-name "${IMAGE_REPO}:${VERSION}" \
    --seed "$MAINTAINER_SIGNING_KEY_PATH" \
    --signer "$SIGNER" \
    --output "dist/image-${VERSION}.sig"

# Step 6: push (optional)
if [ "$PUSH" -eq 1 ]; then
    echo ">>> [6/6] Pushing to Docker Hub..."
    if [ -z "${DOCKER_HUB_USER:-}" ] || [ -z "${DOCKER_HUB_TOKEN:-}" ]; then
        echo "FATAL: DOCKER_HUB_USER/DOCKER_HUB_TOKEN not set" >&2
        exit 4
    fi
    echo "$DOCKER_HUB_TOKEN" | docker login -u "$DOCKER_HUB_USER" --password-stdin
    docker push "$DOCKER_TAG_PINNED"
    docker push "$DOCKER_TAG_LATEST"
    echo ""
    echo "    pushed: $DOCKER_TAG_PINNED"
    echo "    pushed: $DOCKER_TAG_LATEST"
    echo ""
    echo "    :stable NOT pushed — requires K>=2 maintainer signatures."
    echo "    See docs/GOVERNANCE.md for the promote-to-stable procedure."
else
    echo ">>> [6/6] --push not set, skipping Docker Hub push"
    echo "    to push: $0 --signer $SIGNER --push"
fi

echo ""
echo "=========================================="
echo "Release artifacts:"
echo "  dist/${WHEEL_NAME}"
echo "  dist/${WHEEL_NAME}.sig"
echo "  dist/image-${VERSION}.sig"
echo "=========================================="
