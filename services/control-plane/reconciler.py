"""K8s reconciler — background task that reconciles workspace resources.

Polls workspace records every 30s, creates/deletes K8s resources,
and transitions workspace states.
"""

import asyncio
import ipaddress
import logging
import os
import secrets
import socket
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional
import httpx
from kubernetes import client, config
from kubernetes.client.rest import ApiException
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from audit import record_audit_event
from database import async_session_factory
from models import User, Workspace, WorkspaceSecret
from secrets_store import decrypt_value, decrypt_value_if_encrypted, encrypt_if_stable

logger = logging.getLogger("control-plane.reconciler")

RECONCILE_INTERVAL = 30  # seconds
IDLE_GRACE_SECONDS = 30  # grace period from idle_pending → hibernating
def _env_int(name: str, default: int) -> int:
    """Read an int env var, falling back to the default on garbage/missing."""
    try:
        return int(os.environ.get(name, ""))
    except (TypeError, ValueError):
        return default


# A workspace stuck in "starting" past this is failed (restartable).
START_TIMEOUT_MINUTES = _env_int("START_TIMEOUT_MINUTES", 10)
# Namespace deletion is polled to completion; past this we log loudly and
# keep retrying (never claim "deleted" while the namespace still exists).
DELETE_TIMEOUT_MINUTES = _env_int("DELETE_TIMEOUT_MINUTES", 5)

# Concurrent-start capacity: /start and the reconciler's requested→starting
# transition only fire while fewer than MAX_CONCURRENT_STARTS OTHER
# workspaces are 'starting'. Enforced with an atomic conditional UPDATE (the
# count is evaluated inside the UPDATE), so concurrent starts can never
# oversubscribe capacity — no check-then-update race.
MAX_CONCURRENT_STARTS = _env_int("MAX_CONCURRENT_STARTS", 4)
# Per-workspace error backoff: after a reconcile exception the workspace is
# skipped until 30s · 2^failures (capped) has elapsed since the failure.
RECONCILE_BACKOFF_BASE_SECONDS = 30
RECONCILE_BACKOFF_CAP_SECONDS = _env_int("RECONCILE_BACKOFF_CAP_SECONDS", 300)
# user_id → (consecutive_failures, last_error_at). Module-level so all
# reconciler instances share one view; entries are cleared on a clean pass.
RECONCILE_BACKOFF: dict = {}
WORKSPACE_IMAGE = os.environ.get(
    "WORKSPACE_IMAGE",
    "localhost:5000/agent-workspace:dev-latest",
)
CONTROL_PLANE_URL = os.environ.get(
    "CONTROL_PLANE_URL",
    # Workspace pods live in per-user namespaces; use the namespace-qualified
    # control-plane service DNS so usage reporting doesn't NXDOMAIN.
    "http://control-plane.agent-platform.svc.cluster.local:80",
)

def _new_canvas_key() -> str:
    """Generate a per-workspace Canvas credential in its at-rest form
    (Fernet-encrypted when SECRETS_MASTER_KEY is stable)."""
    return encrypt_if_stable(secrets.token_hex(32))

# K8s resource name helpers
def _ns_name(user_id: str) -> str:
    # user_id is a UUID; use it as-is in ws-{uuid}
    short = user_id.replace("-", "")[:12]
    return f"ws-{short}"


def _pvc_name(user_id: str) -> str:
    short = user_id.replace("-", "")[:12]
    return f"home-{short}"


def _deploy_name(user_id: str) -> str:
    short = user_id.replace("-", "")[:12]
    return f"workspace-{short}"


def _svc_name(user_id: str) -> str:
    short = user_id.replace("-", "")[:12]
    return f"workspace-{short}"


PASEO_PASSWORD = os.environ.get("PASEO_PASSWORD", str(uuid.uuid4()))
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")


class Reconciler:
    """Background reconciler that runs as an asyncio task."""

    def __init__(self):
        self._k8s_apps: Optional[client.AppsV1Api] = None
        self._k8s_core: Optional[client.CoreV1Api] = None
        self._k8s_net: Optional[client.NetworkingV1Api] = None
        self._initialized = False
        # user_id → when its namespace deletion was first requested; used to
        # detect namespaces stuck in Terminating past DELETE_TIMEOUT_MINUTES.
        self._ns_delete_started: dict = {}

    def _init_k8s(self):
        if self._initialized:
            return
        try:
            config.load_incluster_config()
        except Exception:
            config.load_kube_config()
        self._k8s_core = client.CoreV1Api()
        self._k8s_apps = client.AppsV1Api()
        self._k8s_net = client.NetworkingV1Api()
        self._initialized = True

    # ─── Namespace ──────────────────────────────────────────────────────

    def _ensure_namespace(self, user_id: str) -> bool:
        """Ensure the workspace namespace exists and is usable.

        Returns True when the namespace is present (or was just created);
        False when it exists but is phase=Terminating — callers must NOT
        create resources into a dying namespace and should retry next pass.
        """
        ns = _ns_name(user_id)
        try:
            obj = self._k8s_core.read_namespace(ns)
            status = getattr(obj, "status", None)
            phase = getattr(status, "phase", None) if status is not None else None
            if phase == "Terminating":
                logger.warning(
                    "Namespace %s is Terminating — skipping resource creation for %s (retry next pass)",
                    ns, user_id,
                )
                return False
            logger.debug("Namespace %s exists", ns)
            return True
        except ApiException as e:
            if e.status != 404:
                raise
            body = client.V1Namespace(
                metadata=client.V1ObjectMeta(name=ns, labels={"workspace": "true", "user-id": user_id}),
            )
            self._k8s_core.create_namespace(body)
            logger.info("Created namespace %s", ns)
            return True

    def _delete_namespace(self, user_id: str):
        ns = _ns_name(user_id)
        try:
            self._k8s_core.delete_namespace(ns)
            logger.info("Deleted namespace %s", ns)
        except ApiException as e:
            if e.status != 404:
                raise

    # ─── PVC ────────────────────────────────────────────────────────────

    def _ensure_pvc(self, user_id: str):
        ns = _ns_name(user_id)
        name = _pvc_name(user_id)
        try:
            self._k8s_core.read_namespaced_persistent_volume_claim(name, ns)
        except ApiException as e:
            if e.status != 404:
                raise
            body = client.V1PersistentVolumeClaim(
                metadata=client.V1ObjectMeta(name=name),
                spec=client.V1PersistentVolumeClaimSpec(
                    access_modes=["ReadWriteOnce"],
                    resources=client.V1ResourceRequirements(
                        requests={"storage": "10Gi"},
                    ),
                    storage_class_name="local-path",
                ),
            )
            self._k8s_core.create_namespaced_persistent_volume_claim(ns, body)
            logger.info("Created PVC %s in %s", name, ns)

    def _delete_pvc(self, user_id: str):
        ns = _ns_name(user_id)
        name = _pvc_name(user_id)
        try:
            self._k8s_core.delete_namespaced_persistent_volume_claim(name, ns)
            logger.info("Deleted PVC %s in %s", name, ns)
        except ApiException as e:
            if e.status != 404:
                raise

    # ─── ServiceAccount ─────────────────────────────────────────────────

    def _ensure_service_account(self, user_id: str):
        ns = _ns_name(user_id)
        name = "workspace"
        try:
            self._k8s_core.read_namespaced_service_account(name, ns)
        except ApiException as e:
            if e.status != 404:
                raise
            body = client.V1ServiceAccount(
                metadata=client.V1ObjectMeta(name=name),
            )
            self._k8s_core.create_namespaced_service_account(ns, body)
            logger.info("Created ServiceAccount %s in %s", name, ns)

    # ─── Service ────────────────────────────────────────────────────────

    def _ensure_service(self, user_id: str):
        ns = _ns_name(user_id)
        name = _svc_name(user_id)
        try:
            self._k8s_core.read_namespaced_service(name, ns)
        except ApiException as e:
            if e.status != 404:
                raise
            body = client.V1Service(
                metadata=client.V1ObjectMeta(name=name),
                spec=client.V1ServiceSpec(
                    selector={"workspace": user_id},
                    ports=[
                        client.V1ServicePort(name="paseo", port=6767, target_port=6767),
                        client.V1ServicePort(name="code-server", port=8080, target_port=8080),
                        client.V1ServicePort(name="canvas", port=8000, target_port=8000),
                        client.V1ServicePort(name="agent", port=9000, target_port=9000),
                    ],
                    type="ClusterIP",
                ),
            )
            self._k8s_core.create_namespaced_service(ns, body)
            logger.info("Created Service %s in %s", name, ns)

    # ─── Deployment ─────────────────────────────────────────────────────

    def _ensure_deployment(
        self,
        user_id: str,
        image: str,
        replicas: int = 1,
        canvas_api_key: str = "",
        canvas_secret_key: str = "",
        agent_token: str = "",
        extra_env: Optional[list] = None,
    ):
        ns = _ns_name(user_id)
        name = _deploy_name(user_id)
        try:
            deploy = self._k8s_apps.read_namespaced_deployment(name, ns)
            # Scale if needed
            if deploy.spec.replicas != replicas:
                deploy.spec.replicas = replicas
                self._k8s_apps.patch_namespaced_deployment(name, ns, deploy)
                logger.info("Scaled Deployment %s in %s to %d", name, ns, replicas)
            return
        except ApiException as e:
            if e.status != 404:
                raise

        # Create Deployment
        labels = {"workspace": user_id, "app": "agent-platform"}
        pod_spec = client.V1PodSpec(
            service_account_name="workspace",
            containers=[
                client.V1Container(
                    name="paseo",
                    image=image,
                    ports=[
                        client.V1ContainerPort(container_port=6767),
                        client.V1ContainerPort(container_port=8080),
                        client.V1ContainerPort(container_port=9000),
                        client.V1ContainerPort(container_port=8000),
                    ],
                    env=[
                        client.V1EnvVar(name="PASEO_PASSWORD", value=PASEO_PASSWORD),
                        client.V1EnvVar(name="ANTHROPIC_API_KEY", value=ANTHROPIC_API_KEY or ""),
                        client.V1EnvVar(name="AGENT_PORT", value="9000"),
                        client.V1EnvVar(name="OH_CANVAS_SAFE_STATE_DIR", value="/workspace/.openhands"),
                        # Match the image's pre-warmed uv cache path (Dockerfile)
                        client.V1EnvVar(name="UV_CACHE_DIR", value="/opt/uv-cache"),
                        # Pin the agent-server version so the runtime uvx spec
                        # matches the image pre-warm exactly (cache hit)
                        client.V1EnvVar(name="OH_AGENT_SERVER_VERSION", value="1.37.0"),
                        # Pin Canvas credentials so reconnect survives pod recreate
                        client.V1EnvVar(name="LOCAL_BACKEND_API_KEY", value=canvas_api_key),
                        client.V1EnvVar(name="OH_SECRET_KEY", value=canvas_secret_key),
                        # Per-workspace agent token — the ONLY credential the
                        # pod may present to the control plane (X-Workspace-Token).
                        client.V1EnvVar(name="WORKSPACE_AGENT_TOKEN", value=agent_token),
                        client.V1EnvVar(name="CONTROL_PLANE_URL", value=CONTROL_PLANE_URL),
                    ] + (extra_env or []),
                    resources=client.V1ResourceRequirements(
                        requests={"cpu": "512m", "memory": "1Gi"},
                        limits={"cpu": "2", "memory": "4Gi"},
                    ),
                    volume_mounts=[
                        client.V1VolumeMount(name="workspace", mount_path="/workspace"),
                    ],
                )
            ],
            volumes=[
                client.V1Volume(
                    name="workspace",
                    persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(
                        claim_name=_pvc_name(user_id),
                    ),
                )
            ],
        )
        body = client.V1Deployment(
            metadata=client.V1ObjectMeta(name=name, labels=labels),
            spec=client.V1DeploymentSpec(
                replicas=replicas,
                selector=client.V1LabelSelector(match_labels={"workspace": user_id}),
                template=client.V1PodTemplateSpec(
                    metadata=client.V1ObjectMeta(labels={"workspace": user_id}),
                    spec=pod_spec,
                ),
            ),
        )
        self._k8s_apps.create_namespaced_deployment(ns, body)
        logger.info("Created Deployment %s in %s (replicas=%d)", name, ns, replicas)

    def _scale_deployment(self, user_id: str, replicas: int):
        """Scale an existing deployment's replicas.

        ApiException(404) is NOT swallowed: a missing deployment means the
        workspace's Deployment was deleted out from under it — callers
        recreate it instead of leaving the workspace permanently bricked.
        """
        ns = _ns_name(user_id)
        name = _deploy_name(user_id)
        body = {"spec": {"replicas": replicas}}
        # Detect a deleted Deployment deterministically (patch alone may
        # 404 in real K8s but not in all clients); 404 propagates to the
        # caller, which recreates the Deployment (H4 recovery).
        self._k8s_apps.read_namespaced_deployment(name, ns)
        self._k8s_apps.patch_namespaced_deployment(name, ns, body)
        logger.info("Scaled Deployment %s in %s to %d (patch)", name, ns, replicas)

    def _delete_deployment(self, user_id: str):
        ns = _ns_name(user_id)
        name = _deploy_name(user_id)
        try:
            self._k8s_apps.delete_namespaced_deployment(name, ns)
            logger.info("Deleted Deployment %s", name)
        except ApiException as e:
            if e.status != 404:
                raise

    def _delete_service(self, user_id: str):
        ns = _ns_name(user_id)
        name = _svc_name(user_id)
        try:
            self._k8s_core.delete_namespaced_service(name, ns)
        except ApiException as e:
            if e.status != 404:
                raise

    def _delete_network_policy(self, user_id: str):
        ns = _ns_name(user_id)
        name = "default-deny-ingress"
        try:
            self._k8s_net.delete_namespaced_network_policy(name, ns)
        except ApiException as e:
            if e.status != 404:
                raise

    def _delete_resource_quota(self, user_id: str):
        ns = _ns_name(user_id)
        name = f"quota-{_ns_name(user_id)}"
        try:
            self._k8s_core.delete_namespaced_resource_quota(name, ns)
        except ApiException as e:
            if e.status != 404:
                raise

    def _delete_service_account(self, user_id: str):
        ns = _ns_name(user_id)
        name = "workspace"
        try:
            self._k8s_core.delete_namespaced_service_account(name, ns)
        except ApiException as e:
            if e.status != 404:
                raise

    # ─── ResourceQuota ─────────────────────────────────────────────────

    def _ensure_resource_quota(self, user_id: str):
        ns = _ns_name(user_id)
        name = f"quota-{_ns_name(user_id)}"
        try:
            self._k8s_core.read_namespaced_resource_quota(name, ns)
        except ApiException as e:
            if e.status != 404:
                raise
            body = client.V1ResourceQuota(
                metadata=client.V1ObjectMeta(name=name),
                spec=client.V1ResourceQuotaSpec(
                    hard={
                        "cpu": "4",
                        "memory": "8Gi",
                        "persistentvolumeclaims": "1",
                        "pods": "2",
                        "requests.storage": "10Gi",
                    },
                ),
            )
            self._k8s_core.create_namespaced_resource_quota(ns, body)
            logger.info("Created ResourceQuota %s in %s", name, ns)

    # ─── NetworkPolicy ──────────────────────────────────────────────────

    def _ensure_network_policy(self, user_id: str, mode: str = "open", allowlist: Optional[list] = None):
        """Apply the workspace's egress mode as a NetworkPolicy.

        open      — deny ingress (platform only); egress unrestricted
        offline   — deny ALL egress (and ingress as in open)
        allowlist — deny egress except DNS, the platform namespace, and the
                    configured hosts/CIDRs (hostnames resolved to IPs at
                    apply time; self-heals on each reconcile)

        Created on first pass; patched when the desired spec differs (e.g.
        after a mode change on a running workspace).
        """
        ns = _ns_name(user_id)
        name = "default-deny-ingress"
        body = self._build_network_policy(name, mode, allowlist)
        try:
            existing = self._k8s_net.read_namespaced_network_policy(name, ns)
            if existing.to_dict().get("spec") != body.to_dict().get("spec"):
                self._k8s_net.patch_namespaced_network_policy(name, ns, body)
                logger.info("Patched NetworkPolicy %s in %s (mode=%s)", name, ns, mode)
            return
        except ApiException as e:
            if e.status != 404:
                raise
        self._k8s_net.create_namespaced_network_policy(ns, body)
        logger.info("Created NetworkPolicy %s in %s (mode=%s)", name, ns, mode)

    def _build_network_policy(self, name: str, mode: str, allowlist: Optional[list]) -> client.V1NetworkPolicy:
        """Build the desired NetworkPolicy for an egress mode."""
        # Ingress is locked to the gateway (full proxy surface: canvas
        # :8000, code-server :8080, paseo :6767, agent :9000) plus the
        # control-plane's readiness probe (:9000 only). Everything else in
        # the platform namespace is denied — because policy_types includes
        # Ingress, any peer without a matching rule is dropped.
        ingress = [
            client.V1NetworkPolicyIngressRule(
                _from=[
                    client.V1NetworkPolicyPeer(
                        namespace_selector=client.V1LabelSelector(
                            match_labels={"kubernetes.io/metadata.name": "agent-platform"},
                        ),
                        pod_selector=client.V1LabelSelector(
                            match_labels={"app.kubernetes.io/name": "gateway"},
                        ),
                    ),
                ],
            ),
            client.V1NetworkPolicyIngressRule(
                ports=[client.V1NetworkPolicyPort(port=9000, protocol="TCP")],
                _from=[
                    client.V1NetworkPolicyPeer(
                        namespace_selector=client.V1LabelSelector(
                            match_labels={"kubernetes.io/metadata.name": "agent-platform"},
                        ),
                        pod_selector=client.V1LabelSelector(
                            match_labels={"app.kubernetes.io/name": "control-plane"},
                        ),
                    ),
                ],
            ),
        ]
        egress = None
        policy_types = ["Ingress"]

        if mode == "offline":
            # Deny all egress: policy present with an empty egress list.
            policy_types = ["Ingress", "Egress"]
            egress = []
        elif mode == "allowlist":
            policy_types = ["Ingress", "Egress"]
            egress = [
                # Cluster DNS (kube-dns/coredns pods)
                client.V1NetworkPolicyEgressRule(
                    to=[
                        client.V1NetworkPolicyPeer(
                            pod_selector=client.V1LabelSelector(
                                match_labels={"k8s-app": "kube-dns"},
                            ),
                        ),
                    ],
                    ports=[client.V1NetworkPolicyPort(port=53, protocol="UDP")],
                ),
                # Control plane + gateway (platform namespace)
                client.V1NetworkPolicyEgressRule(
                    to=[
                        client.V1NetworkPolicyPeer(
                            namespace_selector=client.V1LabelSelector(
                                match_labels={"kubernetes.io/metadata.name": "agent-platform"},
                            ),
                        ),
                    ],
                ),
            ]
            for entry in allowlist or []:
                entry = entry.strip()
                try:
                    ipaddress.ip_network(entry, strict=False)
                    egress.append(client.V1NetworkPolicyEgressRule(
                        to=[client.V1NetworkPolicyPeer(ip_block=client.V1IPBlock(cidr=entry))],
                    ))
                    continue
                except ValueError:
                    pass
                # Hostname → current IPs (self-heals on next reconcile).
                try:
                    ips = set(socket.gethostbyname_ex(entry)[2]) | {socket.gethostbyname(entry)}
                except Exception:
                    logger.warning("Could not resolve allowlist host %s — skipping", entry)
                    continue
                # Sorted + deduped (the resolution is a set): two builds with
                # the same allowlist must produce byte-identical specs — set
                # iteration order is not stable across processes, which
                # caused a spurious patch on every reconcile.
                for ip in sorted(ips):
                    egress.append(client.V1NetworkPolicyEgressRule(
                        to=[client.V1NetworkPolicyPeer(ip_block=client.V1IPBlock(cidr=f"{ip}/32"))],
                    ))

        return client.V1NetworkPolicy(
            metadata=client.V1ObjectMeta(name=name),
            spec=client.V1NetworkPolicySpec(
                pod_selector=client.V1LabelSelector(),
                policy_types=policy_types,
                ingress=ingress,
                egress=egress,
            ),
        )

    # ─── Pod readiness check ───────────────────────────────────────────

    async def _check_pod_ready_host(self, host: str) -> bool:
        """Probe a workspace agent's /ready endpoint on *host*."""
        url = f"http://{host}:9000/ready"
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(url)
                if resp.status_code == 200:
                    data = resp.json()
                    return data.get("status") == "ready"
                return False
        except Exception:
            return False

    async def _check_pod_ready(self, user_id: str) -> bool:
        """Check if the workspace pod is ready by polling /ready."""
        ns = _ns_name(user_id)
        svc_name = _svc_name(user_id)
        return await self._check_pod_ready_host(f"{svc_name}.{ns}.svc.cluster.local")

    async def _get_cluster_ip(self, user_id: str) -> Optional[str]:
        """Get ClusterIP of the workspace service.

        Any failure (no kubeconfig, cluster unreachable, service absent)
        yields None — routing status must never 500 because K8s is not
        reachable.
        """
        ns = _ns_name(user_id)
        try:
            self._init_k8s()
            name = _svc_name(user_id)
            svc = self._k8s_core.read_namespaced_service(name, ns)
            return svc.spec.cluster_ip
        except Exception:
            return None

    # ─── State machine transitions ──────────────────────────────────────

    async def _ensure_agent_token(self, ws: Workspace) -> str:
        """Return the workspace's agent token, generating and persisting it
        when missing (rows created before the column existed). Persisted
        BEFORE the Deployment is created so a crash between persist and
        deploy doesn't lose the token."""
        token = ws.agent_token
        if not token:
            token = secrets.token_urlsafe(32)
            async with async_session_factory() as db:
                await db.execute(
                    update(Workspace)
                    .where(Workspace.workspace_id == ws.workspace_id)
                    .values(agent_token=token)
                )
                await db.commit()
            logger.info("Generated and persisted agent token for workspace %s", ws.workspace_id)
        return token

    async def _build_workspace_env(self, ws: Workspace) -> list:
        """Workspace identity + secrets → env vars (the agent sources
        WS_SECRET_* from these; identity is derived server-side from the
        agent token now)."""
        secret_env: list = []
        username = ""
        async with async_session_factory() as db:
            user_result = await db.execute(select(User).where(User.user_id == ws.user_id))
            user = user_result.scalar_one_or_none()
            if user:
                username = user.username
            result = await db.execute(
                select(WorkspaceSecret).where(WorkspaceSecret.workspace_id == ws.workspace_id)
            )
            for s in result.scalars().all():
                secret_env.append(client.V1EnvVar(
                    name=f"WS_SECRET_{s.key.upper()}",
                    value=decrypt_value(s.value_encrypted),
                ))
        return [
            client.V1EnvVar(name="WORKSPACE_ID", value=ws.workspace_id),
            client.V1EnvVar(name="USERNAME", value=username),
        ] + secret_env

    async def _ensure_workspace_pod(self, ws: Workspace, user_id: str):
        """Create the workspace Deployment (idempotent) with persisted
        credentials. Used by the starting state, and to RECREATE a
        Deployment that vanished while the workspace was running or
        hibernating (H4 recovery)."""
        if not self._ensure_namespace(user_id):
            # Namespace is dying (Terminating): creating resources into it
            # would hot-loop on ApiException. Retry next pass instead.
            logger.warning(
                "Skipping resource creation for workspace %s: namespace %s not usable",
                ws.workspace_id, _ns_name(user_id),
            )
            return
        self._ensure_service_account(user_id)
        self._ensure_pvc(user_id)
        self._ensure_service(user_id)
        self._ensure_resource_quota(user_id)
        # Canvas credentials are stored at rest — Fernet-encrypted when
        # SECRETS_MASTER_KEY is stable, legacy plaintext for pre-encryption
        # rows. Persist the at-rest form unchanged; decrypt only at the
        # pod-env boundary so a ciphertext is never injected as a key.
        stored_api_key = ws.canvas_api_key or _new_canvas_key()
        stored_secret_key = ws.canvas_secret_key or _new_canvas_key()
        if not ws.canvas_api_key or not ws.canvas_secret_key:
            # Persist generated keys so they survive future pod recreates
            async with async_session_factory() as db:
                await db.execute(
                    update(Workspace)
                    .where(Workspace.workspace_id == ws.workspace_id)
                    .values(
                        canvas_api_key=stored_api_key,
                        canvas_secret_key=stored_secret_key,
                    )
                )
                await db.commit()
            logger.info("Persisted Canvas keys for workspace %s", ws.workspace_id)
        agent_token = await self._ensure_agent_token(ws)
        self._ensure_deployment(
            user_id,
            ws.image or WORKSPACE_IMAGE,
            canvas_api_key=decrypt_value_if_encrypted(stored_api_key),
            canvas_secret_key=decrypt_value_if_encrypted(stored_secret_key),
            agent_token=agent_token,
            extra_env=await self._build_workspace_env(ws),
        )

    async def _reconcile_workspace(self, ws: Workspace):
        """Reconcile a single workspace based on its state."""
        user_id = ws.user_id

        if ws.state == "requested":
            # Auto-transition to starting, gated by the SAME conditional
            # UPDATE /start uses (fewer than MAX_CONCURRENT_STARTS OTHER
            # workspaces 'starting'). 0 rows = capacity full: stay requested
            # and retry next pass — no pod resources this pass.
            if not ws.image:
                ws.image = WORKSPACE_IMAGE
            async with async_session_factory() as db:
                result = await db.execute(
                    update(Workspace)
                    .where(Workspace.workspace_id == ws.workspace_id)
                    .where(
                        select(func.count())
                        .select_from(Workspace)
                        .where(
                            Workspace.state == "starting",
                            Workspace.workspace_id != ws.workspace_id,
                        )
                        .scalar_subquery()
                        < MAX_CONCURRENT_STARTS
                    )
                    .values(state="starting", image=ws.image, started_at=datetime.now(timezone.utc))
                )
                if result.rowcount == 0:
                    logger.info(
                        "Workspace %s: starting capacity reached, deferring",
                        ws.workspace_id,
                    )
                    return
                await db.commit()
            logger.info("Workspace %s transitioning requested -> starting", ws.workspace_id)
        if ws.state == "starting":
            # Start deadline: a workspace stuck in starting past
            # START_TIMEOUT_MINUTES is failed (restartable via /start). A
            # missing started_at is treated as fresh (nothing to time out).
            if ws.started_at:
                started_seconds = (datetime.now(timezone.utc) - ws.started_at).total_seconds()
                if started_seconds > START_TIMEOUT_MINUTES * 60:
                    async with async_session_factory() as db:
                        await db.execute(
                            update(Workspace)
                            .where(Workspace.workspace_id == ws.workspace_id)
                            .values(state="failed")
                        )
                        await db.commit()
                    logger.error(
                        "Workspace %s failed: stuck in starting for %.0fs (limit %d min)",
                        ws.workspace_id, started_seconds, START_TIMEOUT_MINUTES,
                    )
                    return
            self._init_k8s()
            try:
                await self._ensure_workspace_pod(ws, user_id)
            except Exception as e:
                logger.error("Failed to create K8s resources for workspace %s: %s", ws.workspace_id, e)
                return  # Will be retried on next poll

            # Check pod readiness
            ready = await self._check_pod_ready(user_id)
            if ready:
                async with async_session_factory() as db:
                    now = datetime.now(timezone.utc)
                    await db.execute(
                        update(Workspace)
                        .where(Workspace.workspace_id == ws.workspace_id)
                        .values(state="running", started_at=now, last_activity_at=now)
                    )
                    await db.commit()
                    await record_audit_event(
                        db, "workspace.ready",
                        actor_user_id=user_id,
                        workspace_id=ws.workspace_id,
                    )
                    await db.commit()
                logger.info("Workspace %s transitioned to running", ws.workspace_id)

        elif ws.state == "running":
            # Keep the egress policy in sync with the configured mode (a mode
            # change on a running workspace applies on the next pass).
            try:
                self._init_k8s()
                self._ensure_network_policy(user_id, mode=ws.network_mode, allowlist=ws.egress_allowlist)
            except Exception as e:
                logger.error("Failed to update NetworkPolicy for %s: %s", ws.workspace_id, e)

            # Check idle timeout
            if ws.last_activity_at:
                now = datetime.now(timezone.utc)
                idle_threshold = ws.idle_timeout_minutes * 60
                idle_seconds = (now - ws.last_activity_at).total_seconds()
                if idle_seconds > idle_threshold:
                    async with async_session_factory() as db:
                        await db.execute(
                            update(Workspace)
                            .where(Workspace.workspace_id == ws.workspace_id)
                            .values(state="idle_pending")
                        )
                        await db.commit()
                    logger.info("Workspace %s idle_pending", ws.workspace_id)

            # Also check pod is still alive — could check via agent health
            ready = await self._check_pod_ready(user_id)
            if not ready:
                # Pod might be restarting, check if deployment exists
                self._init_k8s()
                ns = _ns_name(user_id)
                name = _deploy_name(user_id)
                try:
                    deploy = self._k8s_apps.read_namespaced_deployment(name, ns)
                    if deploy.spec.replicas == 0:
                        # Was hibernated externally
                        async with async_session_factory() as db:
                            await db.execute(
                                update(Workspace)
                                .where(Workspace.workspace_id == ws.workspace_id)
                                .values(state="hibernated")
                            )
                            await db.commit()
                except ApiException as e:
                    if e.status == 404:
                        # Deployment was deleted out from under the running
                        # workspace — recreate it so it recovers instead of
                        # bricking in "running" forever (H4).
                        logger.warning("Deployment %s missing for workspace %s; recreating", name, ws.workspace_id)
                        try:
                            await self._ensure_workspace_pod(ws, user_id)
                        except Exception as e2:
                            logger.error("Failed to recreate Deployment for %s: %s", ws.workspace_id, e2)
                    else:
                        logger.warning("Deployment read failed for %s: %s", ws.workspace_id, e)

        elif ws.state == "idle_pending":
            # Two exits from the grace window: fresh activity cancels the
            # pending hibernation (back to running); the grace period
            # elapsing moves it to hibernating.
            if ws.last_activity_at:
                now = datetime.now(timezone.utc)
                idle_seconds = (now - ws.last_activity_at).total_seconds()
                idle_timeout = ws.idle_timeout_minutes * 60
                idle_threshold = idle_timeout + IDLE_GRACE_SECONDS
                if idle_seconds < idle_timeout:
                    # Activity renewed during idle_pending (e.g. a fresh
                    # activity POST) — cancel the pending hibernate.
                    async with async_session_factory() as db:
                        await db.execute(
                            update(Workspace)
                            .where(Workspace.workspace_id == ws.workspace_id)
                            .values(state="running")
                        )
                        await db.commit()
                    logger.info("Workspace %s back to running (fresh activity during idle_pending)", ws.workspace_id)
                elif idle_seconds > idle_threshold:
                    async with async_session_factory() as db:
                        await db.execute(
                            update(Workspace)
                            .where(Workspace.workspace_id == ws.workspace_id)
                            .values(state="hibernating")
                        )
                        await db.commit()
                    logger.info("Workspace %s hibernating", ws.workspace_id)

        elif ws.state == "hibernating":
            self._init_k8s()
            try:
                self._scale_deployment(user_id, 0)
            except ApiException as e:
                if e.status == 404:
                    # Deployment was deleted out from under the workspace —
                    # recreate it so hibernation stays reversible, then scale
                    # down (H4).
                    logger.warning("Deployment for %s missing during hibernate; recreating", user_id)
                    try:
                        await self._ensure_workspace_pod(ws, user_id)
                        self._scale_deployment(user_id, 0)
                    except Exception as e2:
                        logger.error("Failed to recreate Deployment for %s: %s", ws.workspace_id, e2)
                        return
                else:
                    logger.error("Failed to scale down workspace %s: %s", ws.workspace_id, e)
                    return
            except Exception as e:
                logger.error("Failed to scale down workspace %s: %s", ws.workspace_id, e)
                return
            async with async_session_factory() as db:
                await db.execute(
                    update(Workspace)
                    .where(Workspace.workspace_id == ws.workspace_id)
                    .values(state="hibernated")
                )
                await db.commit()
                await record_audit_event(
                    db, "workspace.hibernated",
                    actor_user_id=user_id,
                    workspace_id=ws.workspace_id,
                )
                await db.commit()
            logger.info("Workspace %s hibernated", ws.workspace_id)

        elif ws.state == "deleting":
            self._init_k8s()
            # Plumbed by the admin delete handler (preserve_pvc query flag);
            # read defensively so the reconciler tolerates rows without the
            # attribute/column.
            preserve_pvc = bool(getattr(ws, "preserve_pvc", False))
            if preserve_pvc:
                # Keep the namespace and PVC (data intact, restartable);
                # remove only pod-facing resources.
                try:
                    self._delete_deployment(user_id)
                    self._delete_service(user_id)
                    self._delete_network_policy(user_id)
                    self._delete_resource_quota(user_id)
                    self._delete_service_account(user_id)
                except Exception as e:
                    logger.error("Failed to delete pod-facing resources for %s: %s", ws.workspace_id, e)
                    async with async_session_factory() as db:
                        await db.execute(
                            update(Workspace)
                            .where(Workspace.workspace_id == ws.workspace_id)
                            .values(state="failed")
                        )
                        await db.commit()
                    return
                async with async_session_factory() as db:
                    await db.execute(
                        update(Workspace)
                        .where(Workspace.workspace_id == ws.workspace_id)
                        .values(state="hibernated")
                    )
                    await db.commit()
                    await record_audit_event(
                        db, "workspace.hibernated",
                        actor_user_id=user_id,
                        workspace_id=ws.workspace_id,
                    )
                    await db.commit()
                logger.info("Workspace %s hibernated (PVC preserved)", ws.workspace_id)
                return

            # Full teardown: delete pod-facing resources and the namespace,
            # but never claim "deleted" until the namespace actually reads
            # 404 (a Terminating namespace still exists).
            try:
                self._delete_deployment(user_id)
                self._delete_service(user_id)
                self._delete_namespace(user_id)
                if user_id not in self._ns_delete_started:
                    self._ns_delete_started[user_id] = datetime.now(timezone.utc)
                    logger.info("Workspace %s: namespace deletion requested", ws.workspace_id)
                try:
                    self._k8s_core.read_namespace(_ns_name(user_id))
                except ApiException as e:
                    if e.status != 404:
                        raise
                    # Namespace is gone — deletion complete.
                    self._ns_delete_started.pop(user_id, None)
                    async with async_session_factory() as db:
                        await db.execute(
                            update(Workspace)
                            .where(Workspace.workspace_id == ws.workspace_id)
                            .values(state="deleted")
                        )
                        await db.commit()
                        await record_audit_event(
                            db, "workspace.deleted",
                            actor_user_id=user_id,
                            workspace_id=ws.workspace_id,
                        )
                        await db.commit()
                    logger.info("Workspace %s deleted", ws.workspace_id)
                    return
                # Namespace still exists (likely Terminating): keep polling.
                elapsed = (datetime.now(timezone.utc) - self._ns_delete_started[user_id]).total_seconds()
                if elapsed > DELETE_TIMEOUT_MINUTES * 60:
                    logger.error(
                        "Workspace %s: namespace %s still exists %.0fs after deletion request — "
                        "stuck Terminating? Keeping state=deleting and retrying next pass",
                        ws.workspace_id, _ns_name(user_id), elapsed,
                    )
                else:
                    logger.info(
                        "Workspace %s: namespace %s still terminating, retrying next pass",
                        ws.workspace_id, _ns_name(user_id),
                    )
            except Exception as e:
                logger.error("Failed to delete K8s resources for %s: %s", ws.workspace_id, e)
                async with async_session_factory() as db:
                    await db.execute(
                        update(Workspace)
                        .where(Workspace.workspace_id == ws.workspace_id)
                        .values(state="failed")
                    )
                    await db.commit()
                return

    async def _reconcile_with_backoff(self, ws: Workspace):
        """Reconcile one workspace, honouring the per-workspace error backoff.

        While a workspace is inside its backoff window (recorded after an
        unhandled reconcile exception) its pass is skipped entirely. A pass
        that completes without an exception is clean: it clears any recorded
        failures (benign early returns — e.g. a Terminating namespace —
        are not failures). Exceptions from _reconcile_workspace are recorded
        here and never escape to the loop.
        """
        user_id = ws.user_id
        entry = RECONCILE_BACKOFF.get(user_id)
        if entry is not None:
            failures, last_error_at = entry
            delay = min(
                RECONCILE_BACKOFF_BASE_SECONDS * (2 ** failures),
                RECONCILE_BACKOFF_CAP_SECONDS,
            )
            if datetime.now(timezone.utc) < last_error_at + timedelta(seconds=delay):
                logger.info(
                    "Workspace %s in error backoff (%d failures) — skipping this pass",
                    ws.workspace_id, failures,
                )
                return
        try:
            await self._reconcile_workspace(ws)
        except Exception as e:
            failures, _ = RECONCILE_BACKOFF.get(user_id, (0, None))
            RECONCILE_BACKOFF[user_id] = (failures + 1, datetime.now(timezone.utc))
            logger.error("Reconcile error for %s: %s", ws.workspace_id, e)
            return
        RECONCILE_BACKOFF.pop(user_id, None)

    # ─── Main loop ──────────────────────────────────────────────────────

    async def run(self):
        """Main reconciler loop. Runs forever."""
        logger.info("Reconciler started")
        while True:
            try:
                async with async_session_factory() as db:
                    result = await db.execute(
                        select(Workspace).where(
                            Workspace.state.in_([
                                "requested", "starting", "running", "idle_pending",
                                "hibernating", "deleting",
                            ])
                        )
                    )
                    workspaces = result.scalars().all()

                    try:
                        await self._reconcile_with_backoff(ws)
                    except Exception as e:
                        logger.error("Reconcile error for %s: %s", ws.workspace_id, e)

            except Exception as e:
                logger.error("Reconciler poll error: %s", e)

            await asyncio.sleep(RECONCILE_INTERVAL)


# Singleton reconciler
reconciler = Reconciler()
