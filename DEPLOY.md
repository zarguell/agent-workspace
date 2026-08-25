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
| `CONTROL_PLANE_URL` | `http://control-plane:80` | Where the workspace agent reports usage (injected into pods) |
| `REPORT_INTERVAL` | `300` | Seconds between workspace-agent usage reports (compute + storage) |
| `OIDC_ISSUER` | — | OIDC provider issuer URL (enables SSO when set with the client creds below) |
| `OIDC_DISCOVERY_URL` | `<issuer>/.well-known/openid-configuration` | Where the control plane fetches the discovery document (override when the browser-facing issuer differs from the URL this container can reach) |
| `OIDC_CLIENT_ID` | — | OIDC client ID registered with the provider |
| `OIDC_CLIENT_SECRET` | — | OIDC client secret |
| `OIDC_REDIRECT_URI` | — | Callback URL registered with the provider (e.g. `https://agents.example.com/api/oidc/callback`) |
| `OIDC_ADMIN_EMAILS` | — | Comma-separated emails; matching SSO users are created as admins |
| `OIDC_SCOPES` | `openid profile email` | Scopes requested from the provider |

### Single sign-on (OIDC)

SSO is implemented with Authlib (authorization-code flow + PKCE S256) and
joserfc (ID-token verification against the provider's JWKS: RS256/384/512 and
ES256/384/512 signatures, expiry, issuer, audience and nonce). It is generic
and works with any provider that publishes an OIDC discovery document —
Google, Microsoft Entra, Keycloak, Auth0, Okta, GitHub, GitLab, ...

Enable it by setting `OIDC_ISSUER`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`
and `OIDC_REDIRECT_URI` on the control plane. The login page then shows a
"Sign in with SSO" button.

Provider setup:

1. Register a public/confidential OAuth client with the provider.
2. Set the redirect URI to `<platform-url>/api/oidc/callback` (e.g.
   `https://agents.example.com/api/oidc/callback`) and authorize the
   `openid`, `profile` and `email` scopes.
3. Configure the control plane with the provider's issuer URL and the
   client credentials.

On first sign-in the control plane provisions the platform user from the
ID-token claims (`sub` is the stable key; the username is derived from
`preferred_username` or the email local-part) and auto-creates their
workspace, matching local-login behavior. Users whose email appears in
`OIDC_ADMIN_EMAILS` are provisioned as admins. Repeated sign-ins reuse the
same account; password login is untouched and can stay enabled alongside
SSO.

OIDC discovery is fetched from `OIDC_DISCOVERY_URL` (default:
`OIDC_ISSUER + "/.well-known/openid-configuration"`). Set it explicitly when
the issuer is only reachable from the browser but the discovery/token
endpoints must be fetched over a different URL — the included mock IdP
(`scripts/mock-oidc-idp.py`, wired into the compose stack) is one such case:
the browser uses `http://127.0.0.1:18099` while the control plane reaches
the provider as `mock-idp` on the compose network.

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

## Backup & Restore

The control plane's Postgres holds all platform state (users, workspaces,
groups, shares, usage, encrypted secrets, MCP registrations, audit). The
workspace *data* itself (agent state, files) lives on the workspace PVCs and
is not covered here — back that up at the cluster/storage layer.

### Compose deployments

```bash
scripts/backup.sh                 # dumps to backups/agentplatform-<stamp>.sql, keeps last 10
BACKUP_DIR=/path/to/dir KEEP=30 scripts/backup.sh

scripts/restore.sh                                # newest dump in backups/
scripts/restore.sh backups/agentplatform-<stamp>.sql
```

`restore.sh` wipes the public schema and re-imports the dump, so the
database ends up exactly as it was at backup time. Restart the control
plane afterwards to re-run its idempotent migrations (they are no-ops on a
restored schema). Sessions survive the restore — they are part of the dump.

Disaster-recovery sequence:

```bash
scripts/backup.sh
docker compose down -v          # destroys the postgres volume
docker compose up -d
scripts/restore.sh
docker compose restart control-plane
```

> **Major Postgres upgrades (e.g. `16-alpine` → `18-alpine`):** PostgreSQL
> major versions cannot share a data directory, and PG18 enables data checksums
> by default at `initdb`. The compose volume is version-pinned (`pgdata18`) so a
> fresh checkout always starts from a clean PG18 directory; the previous `pgdata`
> volume is retired (never corrupted). For the exact fresh-start vs `pg_upgrade`
> steps — including the checksums caveat — see
> [`docs/postgres-major-upgrade.md`](docs/postgres-major-upgrade.md).

### Kubernetes deployments

```bash
kubectl exec -i deploy/agent-platform-postgres -- pg_dump -U agent -d agentplatform > backup.sql
kubectl exec -i deploy/agent-platform-postgres -- psql -U agent -d agentplatform < backup.sql
```

Encrypted secrets remain readable after restore only if `SECRETS_MASTER_KEY`
is unchanged — the encrypted blobs are part of the dump.

### Egress control (network modes)

Each workspace has a network mode, enforced by the reconciler as a
namespace NetworkPolicy (`default-deny-ingress`):

| Mode | Egress |
|---|---|
| `open` (default) | Unrestricted; ingress denied except the platform namespace |
| `offline` | Denied entirely (no DNS, no external network) |
| `allowlist` | DNS (kube-dns) + the platform namespace + configured hosts/CIDRs |

Set it per workspace (operate permission); the reconciler applies the
change on its next pass, including on running workspaces (~30s):

```bash
curl -X PATCH /api/workspaces/<workspace_id>/network \
  -H "Content-Type: application/json" \
  -d '{"mode":"allowlist","allowlist":["pypi.org","files.pythonhosted.org","10.0.0.0/8"]}'
```

Allowlist entries are CIDRs (used as-is) or hostnames (resolved to their
current IPs at apply time; re-resolved on each reconcile so drift
self-heals). The "offline + package cache" setup is: `mode: allowlist`
with only your package proxy's CIDR/hostname — the agent reaches the
cache and nothing else. For an agent that needs no network at all, use
`mode: offline`.
