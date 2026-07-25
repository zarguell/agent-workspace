#!/bin/bash
# Build all services for the agent platform.
# Run from the repo root (projects/agent-workspace).
set -euo pipefail

REGISTRY="${REGISTRY:-localhost:5000}"
TAG="${TAG:-dev}"

echo "Building with REGISTRY=$REGISTRY TAG=$TAG"

# ── Workspace image prerequisites ──────────────────────────────────────
# Pre-downloaded binaries avoid curl DNS issues inside the Paseo container.
WS_DIR="services/workspace"

echo "--- Downloading workspace prerequisites ---"

# OMP binary
if [ ! -f "$WS_DIR/omp-linux-x64" ]; then
    echo "Downloading OMP CLI..."
    curl -sL -o "$WS_DIR/omp-linux-x64" \
        "https://github.com/can1357/oh-my-pi/releases/download/v17.1.3/omp-linux-x64"
    chmod +x "$WS_DIR/omp-linux-x64"
fi

# code-server .deb
if [ ! -f "$WS_DIR/code-server.deb" ]; then
    echo "Downloading code-server..."
    curl -sL -o "$WS_DIR/code-server.deb" \
        "https://github.com/coder/code-server/releases/download/v4.130.0/code-server_4.130.0_amd64.deb"
fi

# uv tarball
if [ ! -f "$WS_DIR/uv.tar.gz" ]; then
    echo "Downloading uv..."
    curl -sL -o "$WS_DIR/uv.tar.gz" \
        "https://github.com/astral-sh/uv/releases/download/0.11.32/uv-x86_64-unknown-linux-gnu.tar.gz"
fi

echo "--- Prerequisites ready ---"

# ── Build images ───────────────────────────────────────────────────────

build() {
    local name=$1
    local dir=$2
    echo "--- Building $name ---"
    docker build --no-cache -t "$REGISTRY/$name:$TAG" "$dir"
    docker push "$REGISTRY/$name:$TAG"
    echo "--- Done $name ---"
    echo ""
}

build agent-gateway      services/gateway
build agent-control-plane services/control-plane
build agent-workspace     services/workspace

echo "All images built and pushed."
echo ""
echo "Then deploy with:"
echo "  helm upgrade --install agent-platform charts/agent-platform \\"
echo "    -f values.local.yaml --namespace agent-platform"
