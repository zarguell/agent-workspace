# Roadmap

Living document. Open an issue or PR to suggest changes.

## Completed

- [x] Single-user workspace MVP with Agent Canvas chat
- [x] Claude Code via ACP (Agent Client Protocol)
- [x] Path-based routing under one domain
- [x] Session authentication and workspace isolation
- [x] Idle hibernation with PVC persistence
- [x] Standalone control experiment (Phase 1)
- [x] Per-workspace full-stack integration architecture

## Phase 1: Admin & Multi-Tenancy

- [ ] **Admin tools** — user management dashboard, audit logs, workspace monitoring
- [ ] **Group management** — RBAC for workspace resources (who can access which tools, secrets, deployments)
- [ ] **Usage logging** — per-user activity tracking, workspace events, agent execution logs

## Phase 2: MCP & Tool Ecosystem

- [ ] **MCP Gateway** — shared MCP tools accessible across workspaces, gated by RBAC
- [ ] **Resource sharing via MCP** — port forwarding, static site / GitHub Pages-like deployment so agents can publish pages or share dev servers with the team
- [ ] **Self-hosted browser** — templates for deploying tools like Firecrawl or Crawl4AI as workspace add-ons

## Phase 3: Deployment & Infrastructure

- [x] **Local HTTPS** — self-signed cert script + Traefik `websecure` entrypoint (gated by `tls.enabled`)
- [ ] **Production HTTPS** — cert-manager + Let's Encrypt; docs shipped, verify against a live domain
- [ ] **Docker Compose deployment** — single-machine setup without Kubernetes for small teams / evaluation
- [ ] **MicroVM support** — evaluate Firecracker / microVM environments as an alternative to pod-based isolation
- [ ] **Long-running tasks** — lightweight job system that survives workspace hibernation (keep processes running while reclaiming user pod resources)
- [ ] **Backup & restore** — Postgres database backup, PVC snapshots for workspace data

## Phase 4: Cost & Resource Management

- [ ] **Per-user token tracking** — LLM token usage accounting per user/workspace
- [ ] **Cost tracking** — aggregate cost visibility by user, group, or project

## Phase 5: Workspace Flexibility

- [ ] **Image flavors** — users select their agent harness at workspace creation (Claude Code, OpenHands, code-server only, etc.) with prebuilt images per flavor
- [ ] **Image customization guide** — documented process for building custom workspace images
- [ ] **Secrets management** — encrypted environment variable injection (Codespaces-style secrets per user/workspace)
- [ ] **Agent conversation persistence** — verify and harden Canvas conversation state across hibernate/resume cycles

## Phase 6: Security & DLP

- [ ] **DLP policies** — dynamic data masking via Presidio or equivalent, redact API keys, PII, and secrets before they reach LLM providers
- [ ] **Policy-driven access control** — admin-defined rules for what data can be sent to which agents/models
