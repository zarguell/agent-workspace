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

## Local Development (Docker Compose)

The `compose.yaml` at the repo root runs the full control plane + gateway +
Postgres stack locally without Kubernetes. Useful while a cluster is
unavailable, or for UI/API development.

```bash
docker compose up --build
```

Then open `http://localhost:18088/ui/login` (admin credentials:
`SEED_ADMIN_USER` / `SEED_ADMIN_PASSWORD`, defaults `admin`/`admin`).
The gateway is at port `18088`, the control plane at `18011`, Postgres at
`15432` — override with `CONTROL_PLANE_PORT`, `GATEWAY_PORT`,
`POSTGRES_PORT` in a `.env` file next to `compose.yaml`.

Key env vars:

| Variable | Default | Description |
|---|---|---|
| `SERVICE_AUTH_TOKEN` | `local-token` | Shared token; must match between gateway and control plane |
| `DISABLE_RECONCILER` | `1` | Skip the K8s reconciler (no cluster in Compose) |
| `WORKSPACE_DEV_HOST` | — | Fixed workspace host to route to instead of a K8s ClusterIP |
| `SEED_ADMIN_USER` / `SEED_ADMIN_PASSWORD` | `admin` / `admin` | First-run admin seed |

Workspace *pods* remain a Kubernetes concept. To exercise the full
Canvas / code-server flow locally, run a workspace container that exposes
ports `6767` (Paseo), `8080` (code-server), `8000` (Canvas), `9000` (agent),
then set `WORKSPACE_DEV_HOST` to its address (a compose service name or
`host.docker.internal`) and restart:

The control plane then reports that host as the workspace ClusterIP and
probes its `:9000/ready` endpoint for readiness, so the gateway proxies to
it exactly as it would to a pod.

### MCP servers

Workspace MCP (Model Context Protocol) servers are registered per workspace
(control-plane API or the "MCP Servers" panel on the workspaces page) and
served by the gateway at `/mcp/{server_id}` — authenticated with the session
cookie and authorized like any workspace route (owner, admin, or a group
share with operate permission). MCP JSON-RPC requests are proxied verbatim
to `http://{workspace-cluster-ip}:{port}`:

```bash
curl -X POST https://example.com/mcp/<server_id> \
  -H "Content-Type: application/json" \
  -b "session=<cookie>" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

Register a server:

```bash
curl -X POST /api/workspaces/<workspace_id>/mcp-servers \
  -H "Content-Type: application/json" \
  -d '{"name":"my-tools","port":3001}'
```

The gateway strips the `/mcp/{server_id}` prefix before forwarding, so
sub-paths like `/mcp/{server_id}/messages` reach the server as `/messages`.
Disable a server to take it offline without unregistering.

### Workspace secrets

Per-workspace secrets are stored **encrypted at rest** (Fernet) and injected
into workspace pods as `WS_SECRET_<KEY>` environment variables when the pod
is created. Set the master key or existing secrets become unreadable:

| Variable | Default | Description |
|---|---|---|
| `SECRETS_MASTER_KEY` | (ephemeral, with warning) | Fernet key for secret encryption; must be 32 url-safe base64 bytes |

Manage secrets via the workspaces page ("Secrets" panel) or the API:

```bash
curl -X PUT /api/workspaces/<workspace_id>/secrets/GITHUB_TOKEN \
  -H "Content-Type: application/json" -d '{"value":"ghp_..."}'
```

Keys must match `[A-Za-z0-9_-]` (uppercased for the env var name); reading
values requires operate permission, listing names requires view.




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
| `OH_AGENT_SERVER_VERSION` | `1.37.0` | Pinned agent-server version (must match image pre-warm) |
| `UV_CACHE_DIR` | `/opt/uv-cache` | uv cache path (baked into image at build; reused at runtime) |

### Workspace persistence

Canvas credentials (`LOCAL_BACKEND_API_KEY`, `OH_SECRET_KEY`) are generated
per-workspace and stored in Postgres (`workspaces.canvas_api_key`,
`workspaces.canvas_secret_key`), then injected into workspace pods. This keeps
reconnect and settings encryption stable across pod hibernate/recreate — the
keys no longer live on the ephemeral container layer.

The workspace image pre-warms the agent-server Python environment into
`/opt/uv-cache` at build time, so fresh pods skip the multi-minute litellm
build. The reconciler pins `OH_AGENT_SERVER_VERSION` and `UV_CACHE_DIR` to
match the image. If you change the agent-server version in the Dockerfile,
update `reconciler.py`'s `OH_AGENT_SERVER_VERSION` to match.

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

### TLS / HTTPS

| Key | Default | Description |
|---|---|---|
| `tls.enabled` | `false` | Serve HTTPS via the Traefik `websecure` entrypoint |
| `tls.secretName` | `agent-platform-tls` | Name of the TLS secret holding the cert + key |

## HTTPS

### Local deployment (self-signed)

The chart ships with a script that generates a self-signed CA + leaf certificate
and installs it as a Kubernetes TLS secret:

```bash
./scripts/generate-selfsigned-cert.sh agent-platform agents.local.test
```

Then enable TLS in `values.local.yaml` and re-deploy:

```yaml
tls:
  enabled: true
```

```bash
helm upgrade --install agent-platform charts/agent-platform \
  -f charts/agent-platform/values.local.yaml --namespace agent-platform
```

To avoid browser warnings, trust the generated CA certificate on each client
machine (`certs/agent-platform-ca.crt`) — or accept the warning once per browser.
The certificate includes SANs for `{domain}`, `*.{domain}`, `localhost`, and
`127.0.0.1`.

**Ports:** Traefik exposes the `websecure` entrypoint on NodePort `30118` by
default. Forward it the same way you forward the HTTP port — e.g. an SSH tunnel:

```bash
ssh -L 31061:localhost:30118 root@<server>
```

Then access `https://agents.local.test:31061/`.

> The private key lives only in `certs/` (gitignored) and in the in-cluster
> Secret — never commit it.

### Production (Let's Encrypt)

For real certificates, use [cert-manager](https://cert-manager.io) with a
`ClusterIssuer` for Let's Encrypt, then point `tls.secretName` at the
cert-manager-issued secret:

1. Install cert-manager and create a `ClusterIssuer` (HTTP-01 or DNS-01
   challenge; DNS-01 works for wildcard certs).
2. Create a certificate resource for your domain:

```yaml
apiVersion: cert-manager.io/v1
kind: Certificate
metadata:
  name: agent-platform-tls
  namespace: agent-platform
spec:
  secretName: agent-platform-tls
  dnsNames:
    - example.com
    - "*.example.com"
  issuerRef:
    name: letsencrypt-prod
    kind: ClusterIssuer
```

3. Set `tls.enabled: true` in `values.local.yaml` and re-deploy. Traefik will
   serve the cert-manager-issued certificate.

The gateway terminates TLS at Traefik and proxies plain HTTP internally, so no
application code changes are needed for HTTPS.

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
