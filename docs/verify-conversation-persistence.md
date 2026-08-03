# Verification: Conversation Persistence & Reconnect

Procedure to run on a live cluster after deploying the workspace platform.
Validates the two persistence hardening changes:

1. **Pinned Canvas credentials** — `LOCAL_BACKEND_API_KEY` / `OH_SECRET_KEY` are
   now per-workspace values persisted in Postgres (`workspaces.canvas_api_key`,
   `workspaces.canvas_secret_key`) and injected as pod env vars, so a pod
   recreate no longer regenerates the keys (previously they lived in the
   container layer and broke reconnect + settings decryption on hibernate).
2. **Pre-warmed uv cache** — the workspace image now bakes the agent-server
   Python environment into `/opt/uv-cache` at build time, so a fresh pod skips
   the 2-4 minute litellm build on first boot.

## Prerequisites

- Cluster reachable; images built with the new Dockerfile (`TAG` bumped so the
  workspace image is rebuilt — the pre-warm is a Dockerfile change).
- Control plane deployed with the new reconciler/model (migration adds the two
  columns automatically on startup).

## Steps

### 1. Build with the new image

```bash
REGISTRY=localhost:5000 TAG=v-persist ./scripts/build-images.sh
helm upgrade --install agent-platform charts/agent-platform \
  -f charts/agent-platform/values.local.yaml \
  --namespace agent-platform \
  --set images.tag=v-persist
```

Wait for the control-plane rollout, then delete the existing workspace
deployment so it recreates with the new env vars:

```bash
kubectl -n ws-<user> delete deploy workspace-<user>
```

(Recreating the deployment is required only once; existing pods keep their
old env until recreated.)

### 2. Fresh pod boot speed

Start the workspace and measure time from pod creation to `ready`:

```bash
kubectl -n ws-<user> get pods -w
# expect: no "Building litellm==..." in the pod logs
kubectl -n ws-<user> logs deploy/workspace-<user> | grep -i "litellm\|Installed packages" || true
```

**Expected:** no Python env build on first boot; agent-server comes up in
seconds. Record the wall time.

### 3. Conversation survives hibernate

1. Log in as the workspace user in a fresh browser profile.
2. Open `/canvas/`, start a conversation with the agent (a simple prompt).
3. Confirm the conversation renders and messages persist (send 2+ turns).
4. Hibernate the workspace (workspaces page → Hibernate, or API).
5. Wait for the pod to scale to 0:
   ```bash
   kubectl -n ws-<user> get pods  # expect: no pods
   ```
6. Re-open `/canvas/` (or click Start / Open Canvas).

**Expected:**
- The conversation from step 3 is still present (list of past conversations,
  or resumed thread) — state dir is on the PVC.
- Reconnect works without re-onboarding: the browser does not show an API-key
  entry screen, and settings (agent choice, model) are unchanged.
- The Canvas UI connects to the agent server immediately.

### 4. Key stability across recreate

Before hibernation, capture the pod env:

```bash
kubectl -n ws-<user> exec deploy/workspace-<user> -- env | grep -E "LOCAL_BACKEND_API_KEY|OH_SECRET_KEY"
```

After resume, compare with the recreated pod's env — **must be identical**.
Also confirm they match the DB:

```bash
kubectl -n agent-platform exec deploy/control-plane -- \
  python3 -c "import asyncio; from sqlalchemy import select; from database import async_session_factory; from models import Workspace; asyncio.run((lambda: None)())" 2>/dev/null || true
# (or query Postgres directly: SELECT canvas_api_key, canvas_secret_key FROM workspaces;)
```

### 5. Settings encryption

In Canvas Settings, change the agent model (or add a secret). Hibernate +
resume. Open Settings again — **expected:** the change is retained (encrypted
with the pinned `OH_SECRET_KEY`, which did not change).

## Pass criteria

- [ ] Fresh pod boots without the litellm/Python build
- [ ] Conversation survives hibernate → resume
- [ ] Reconnect works without re-onboarding
- [ ] `LOCAL_BACKEND_API_KEY` / `OH_SECRET_KEY` identical across pod recreate
- [ ] Canvas settings persist across hibernate

## Failure diagnosis

| Symptom | Likely cause |
|---|---|
| Still builds litellm on boot | Image not rebuilt (stale Docker layer) or `UV_CACHE_DIR` mismatch between Dockerfile and reconciler |
| Reconnect asks for API key after resume | Pod recreated with different key — check DB column population and reconciler env |
| Settings reset after resume | `OH_SECRET_KEY` changed — pin check failed |
| Conversation gone after resume | `OH_CANVAS_SAFE_STATE_DIR` not on PVC / state dir misconfigured |
