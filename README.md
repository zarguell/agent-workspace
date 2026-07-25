# Agent Workspace Platform

Multi-user, Kubernetes-native workspace platform for AI coding agents. Each user gets their own persistent workspace pod with agentic IDE access — browser-based, session-authenticated, hibernate-on-idle.

## Purpose

Teams need shared access to AI coding agents (Claude Code, Codex, Gemini CLI, OpenHands) without sharing terminals or leaving security boundaries. This platform provides:

- **Per-user workspace pods** — isolated Kubernetes pods with persistent PVC-backed storage
- **Agent Canvas UI** — browser-based chat with Claude Code via ACP (Agent Client Protocol)
- **code-server** — VS Code in the browser as a fallback/debug tool
- **Session auth** — username/password login with cookie-based sessions
- **Idle hibernation** — workspace pods scale to zero after inactivity, PVC persists
- **Single domain** — everything at `{domain}:{port}/canvas/`, `/code/`, `/api/`

## Quick Start

Requirements: Kubernetes cluster (K3s recommended), Helm, local Docker registry.

```bash
git clone https://github.com/your-org/agent-workspace
cd agent-workspace

# Configure your domain
cp charts/agent-platform/values.local.example.yaml charts/agent-platform/values.local.yaml
# Edit values.local.yaml with your domain and secrets

# Build and deploy
./scripts/build-images.sh
helm upgrade --install agent-platform charts/agent-platform \
  -f charts/agent-platform/values.local.yaml --namespace agent-platform
```

See [DEPLOY.md](DEPLOY.md) for full setup instructions.

## Architecture

```
Browser → Traefik → Gateway → Control Plane → Postgres
                              └── Workspace Pod ── PVC (/workspace)
                                   ├── Agent Canvas (:8000 via ACP → Claude Code)
                                   ├── code-server (:8080)
                                   └── workspace-agent (:9000)
```

- **Gateway** — FastAPI middleware that validates session cookies, resolves user workspace, and proxies requests by path prefix (`/canvas/` → `:8000`, `/code/` → `:8080`)
- **Control Plane** — FastAPI + SQLAlchemy for user/session/workspace management, background reconciler for K8s resource lifecycle
- **Workspace Pod** — single container running Agent Canvas (OpenHands Agent Server + UI), code-server, and a health agent
- **PVC** — 10Gi per user, survives pod hibernation

## Roadmap

See [ROADMAP.md](ROADMAP.md).
