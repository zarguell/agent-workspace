# Deployment Guide

Complete setup instructions for the Agent Workspace Platform on a single-node K3s cluster.

## Prerequisites

- Linux server (Debian/Ubuntu recommended) with internet access
- K3s installed (`curl -sfL https://get.k3s.io | sh -`)
- Docker installed on the K3s host (for building images)
- A local Docker registry (run `docker run -d -p 5000:5000 --restart=always registry:2`)
- Domain or DNS entry pointing to your server (or use `/etc/hosts` entries for local testing)

## Quick Start

### 1. Configure DNS

For local testing, add to `/etc/hosts` on each client machine:

```
<server-ip>  example.com
```

Replace `example.com` with your domain in all config files.

### 2. Configure Helm values

```bash
cp charts/agent-platform/values.local.example.yaml charts/agent-platform/values.local.yaml
```

Edit `values.local.yaml`:
- Set `baseDomain` to your domain (e.g., `example.com`)
- Set `postgres.password` to a secure password
- Set `serviceAuthToken` to a secure random token
- Optionally set `anthropicApiKey` for Claude Code agent support

### 3. Build images

```bash
REGISTRY=localhost:5000 TAG=dev ./scripts/build-images.sh
```

This builds and pushes three images:
- `localhost:5000/agent-gateway:dev` — session-aware reverse proxy
- `localhost:5000/agent-control-plane:dev` — API server + K8s reconciler
- `localhost:5000/agent-workspace:dev` — workspace pod (Agent Canvas + code-server)

> **Note**: The workspace image build downloads npm packages (`@openhands/agent-canvas`, `@agentclientprotocol/claude-agent-acp`) and Python dependencies. First build takes 5-10 minutes.

### 4. Deploy

```bash
helm upgrade --install agent-platform charts/agent-platform \
  -f charts/agent-platform/values.local.yaml \
  --namespace agent-platform \
  --set images.tag=dev
```

### 5. Verify

```bash
# Check all pods are running
kubectl -n agent-platform get pods

# Check the API health
curl -H "Host: example.com" http://<node-ip>:<nodeport>/api/health

# Access the platform
```

Open `http://example.com:<nodeport>/` in a browser. Login with the default admin credentials (set via `SEED_ADMIN_USER` / `SEED_ADMIN_PASSWORD` env vars, defaults: `admin`/`admin`).

## Configuration

### Environment Variables

| Variable | Default | Description |
|---|---|---|
| `BASE_DOMAIN` | `example.com` | Base domain for the platform |
| `COOKIE_DOMAIN` | `.example.com` | Domain for session cookies |
| `SEED_ADMIN_USER` | `admin` | Default admin username (first-run seed) |
| `SEED_ADMIN_PASSWORD` | `admin` | Default admin password (first-run seed) |
| `ANTHROPIC_API_KEY` | — | API key for Claude Code ACP agent |
| `PASEO_PASSWORD` | (auto-generated) | Password for Paseo daemon WebSocket auth |
| `WORKSPACE_IMAGE` | `localhost:5000/agent-workspace:dev-latest` | Container image for workspace pods |

### Helm values.yaml

| Key | Default | Description |
|---|---|---|
| `baseDomain` | `example.com` | Base domain |
| `images.controlPlane` | `localhost:5000/agent-control-plane` | Control plane image |
| `images.gateway` | `localhost:5000/agent-gateway` | Gateway image |
| `images.workspace` | `localhost:5000/agent-workspace` | Workspace image |
| `images.tag` | `dev-latest` | Image tag |
| `postgres.password` | `changeme` | Postgres password |
| `serviceAuthToken` | (required) | Internal service auth token |
| `anthropicApiKey` | — | Anthropic API key for Claude Code |
| `idleTimeoutMinutes` | `15` | Workspace idle timeout before hibernation |

## Accessing Services

| URL | Service | Access |
|---|---|---|
| `http://{domain}:{port}/` | Workspaces dashboard | Login required |
| `http://{domain}:{port}/canvas/` | Agent Canvas | Login required, workspace must be running |
| `http://{domain}:{port}/code/` | code-server | Login required, workspace must be running |

## Adding Users

Users can be created via the control-plane API:

```bash
curl -X POST http://{domain}:{port}/api/users \
  -H "Content-Type: application/json" \
  -d '{"username":"newuser","password":"securepass","display_name":"New User"}'
```

Requires admin session.

## Workspace Lifecycle

- Workspaces start on first access (login triggers provisioning)
- After 15 minutes of inactivity, workspace hibernates (pods scale to 0)
- PVC data persists across hibernation cycles
- On next access, workspace resumes automatically

## Troubleshooting

### Gateway returns 404 for workspace paths

Check the Ingress has the required path rules:
```bash
kubectl get ingress -n agent-platform main-ingress
```
Paths should include `/`, `/api`, `/ui`, `/workspace`, `/canvas`, `/code`.

### Workspace stays in "starting" state

The reconciler polls every 30 seconds. Check pod status:
```bash
kubectl -n ws-* get pods
```
If the pod is running but state still says "starting", the readiness check on port 9000 may be failing:
```bash
kubectl -n ws-* exec <pod> -- curl -s localhost:9000/ready
```

### Agent Canvas doesn't load

Check the workspace pod's Agent Canvas startup:
```bash
kubectl -n ws-* logs <pod> | grep -i canvas
```
First startup builds Python dependencies (litellm) — may take 2-4 minutes.

## Updating

```bash
# Rebuild with new tag
REGISTRY=localhost:5000 TAG=v2 ./scripts/build-images.sh

# Deploy
helm upgrade --install agent-platform charts/agent-platform \
  -f charts/agent-platform/values.local.yaml \
  --namespace agent-platform \
  --set images.tag=v2
```
