# Requirements

## Purpose

A multi-user, Kubernetes-native workspace platform where each user gets an isolated, browser-accessible environment for AI coding agents. The platform provisions per-user workspace pods with Agent Canvas (OpenHands Agent Server + UI), code-server, and PVC-backed persistent storage, all behind a session-aware gateway.

## Core Requirements

### Platform

1. **Kubernetes-native** — single-node K3s deployment, Helm chart for all resources
2. **Session authentication** — username/password with bcrypt-hashed passwords, cookie-based sessions
3. **Path-based routing** — all services under one domain: `/canvas/` (Agent Canvas), `/code/` (code-server), `/api/` (control plane)
4. **Workspace lifecycle** — provision on request, hibernate after idle timeout, resume on access
5. **Persistent storage** — PVC-backed `/workspace` per user, survives hibernate cycles
6. **Self-contained** — local Docker registry, no external dependencies

### Workspace Pod

7. **Agent Canvas** (port 8000) — OpenHands Agent Server with SPA frontend, spawns ACP agent subprocesses
8. **code-server** (port 8080) — VS Code in the browser (fallback/debug)
9. **workspace-agent** (port 9000) — health checks, readiness probes, activity tracking
10. **ACP agents** — Claude Code via `@agentclientprotocol/claude-agent-acp`, extendable to Codex, Gemini CLI

### Gateway

11. **Session validation** — validates session cookie against control plane on every proxied request
12. **Workspace resolution** — resolves user → pod IP/port via control plane API
13. **HTML modification** — rewrites root-relative asset paths and injects `<base>` tags for sub-path serving
14. **WebSocket relay** — proxies WebSocket connections for agent-server communication
15. **Hibernation wake** — triggers workspace start and shows auto-refreshing "starting" page

### Control Plane

16. **User management** — create, list users with admin flag
17. **Workspace CRUD** — create, read, hibernate, delete workspaces in Postgres
18. **K8s reconciler** — background loop that reconciles workspace state → K8s resources (30s interval)
19. **Audit trail** — all workspace and auth events logged to Postgres
20. **Idempotency** — idempotency-key dedup for lifecycle operations

## Non-Goals (for MVP)

- Public cloud deployment (AWS, GCP)
- OAuth/SSO
- Multi-cluster or multi-region
- Enterprise RBAC
- Usage billing
- Custom workspace image builder

## Future Considerations

See [ROADMAP.md](ROADMAP.md) for planned work: admin tools, MCP gateway, Docker Compose deployment, DLP policies, and more.
