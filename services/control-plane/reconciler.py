"""K8s reconciler — background task that reconciles workspace resources.

Polls workspace records every 30s, creates/deletes K8s resources,
and transitions workspace states.
"""

import asyncio
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Optional

import httpx
from kubernetes import client, config
from kubernetes.client.rest import ApiException
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from audit import record_audit_event
from database import async_session_factory
from models import Workspace

logger = logging.getLogger("control-plane.reconciler")

RECONCILE_INTERVAL = 30  # seconds
IDLE_GRACE_SECONDS = 30  # grace period from idle_pending → hibernating
WORKSPACE_IMAGE = os.environ.get(
    "WORKSPACE_IMAGE",
    "localhost:5000/agent-workspace:dev-latest",
)
SERVICE_AUTH_TOKEN = os.environ.get("SERVICE_AUTH_TOKEN", "internal-service-token")

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

    def _ensure_namespace(self, user_id: str):
        ns = _ns_name(user_id)
        try:
            self._k8s_core.read_namespace(ns)
            logger.debug("Namespace %s exists", ns)
        except ApiException as e:
            if e.status != 404:
                raise
            body = client.V1Namespace(
                metadata=client.V1ObjectMeta(name=ns, labels={"workspace": "true", "user-id": user_id}),
            )
            self._k8s_core.create_namespace(body)
            logger.info("Created namespace %s", ns)

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

    def _ensure_deployment(self, user_id: str, image: str, replicas: int = 1):
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
                    ],
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
        """Scale an existing deployment's replicas."""
        ns = _ns_name(user_id)
        name = _deploy_name(user_id)
        try:
            body = {"spec": {"replicas": replicas}}
            self._k8s_apps.patch_namespaced_deployment(name, ns, body)
            logger.info("Scaled Deployment %s in %s to %d (patch)", name, ns, replicas)
        except ApiException as e:
            if e.status == 404:
                logger.warning("Deployment %s not found for scaling", name)
                return
            raise

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

    def _ensure_network_policy(self, user_id: str):
        ns = _ns_name(user_id)
        name = "default-deny-ingress"
        try:
            self._k8s_net.read_namespaced_network_policy(name, ns)
        except ApiException as e:
            if e.status != 404:
                raise
            # Deny all ingress except from agent-platform namespace
            body = client.V1NetworkPolicy(
                metadata=client.V1ObjectMeta(name=name),
                spec=client.V1NetworkPolicySpec(
                    pod_selector=client.V1LabelSelector(),
                    policy_types=["Ingress"],
                    ingress=[
                        client.V1NetworkPolicyIngressRule(
                            _from=[
                                client.V1NetworkPolicyPeer(
                                    namespace_selector=client.V1LabelSelector(
                                        match_labels={"kubernetes.io/metadata.name": "agent-platform"},
                                    ),
                                ),
                            ],
                        )
                    ],
                ),
            )
            self._k8s_net.create_namespaced_network_policy(ns, body)
            logger.info("Created NetworkPolicy %s in %s", name, ns)

    # ─── Pod readiness check ───────────────────────────────────────────

    async def _check_pod_ready(self, user_id: str) -> bool:
        """Check if the workspace pod is ready by polling /ready."""
        ns = _ns_name(user_id)
        svc_name = _svc_name(user_id)
        url = f"http://{svc_name}.{ns}.svc.cluster.local:9000/ready"
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(url, headers={"X-Service-Auth": SERVICE_AUTH_TOKEN})
                if resp.status_code == 200:
                    data = resp.json()
                    return data.get("status") == "ready"
                return False
        except Exception:
            return False

    async def _get_cluster_ip(self, user_id: str) -> Optional[str]:
        """Get ClusterIP of the workspace service."""
        ns = _ns_name(user_id)
        self._init_k8s()
        name = _svc_name(user_id)
        try:
            svc = self._k8s_core.read_namespaced_service(name, ns)
            return svc.spec.cluster_ip
        except ApiException:
            return None

    # ─── State machine transitions ──────────────────────────────────────

    async def _reconcile_workspace(self, ws: Workspace):
        """Reconcile a single workspace based on its state."""
        user_id = ws.user_id

        if ws.state == "requested":
            # Auto-transition to starting
            if not ws.image:
                ws.image = WORKSPACE_IMAGE
            async with async_session_factory() as db:
                await db.execute(
                    update(Workspace)
                    .where(Workspace.workspace_id == ws.workspace_id)
                    .values(state="starting", image=ws.image)
                )
                await db.commit()
            logger.info("Workspace %s transitioning requested -> starting", ws.workspace_id)

        if ws.state == "starting":
            self._init_k8s()
            try:
                self._ensure_namespace(user_id)
                self._ensure_service_account(user_id)
                self._ensure_pvc(user_id)
                self._ensure_service(user_id)
                self._ensure_resource_quota(user_id)
                self._ensure_network_policy(user_id)
                self._ensure_deployment(user_id, ws.image or WORKSPACE_IMAGE, replicas=1)
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
                except ApiException:
                    pass

        elif ws.state == "idle_pending":
            # Check if grace period elapsed
            if ws.last_activity_at:
                now = datetime.now(timezone.utc)
                idle_seconds = (now - ws.last_activity_at).total_seconds()
                idle_threshold = (ws.idle_timeout_minutes * 60) + IDLE_GRACE_SECONDS
                if idle_seconds > idle_threshold:
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
            try:
                self._delete_deployment(user_id)
                self._delete_service(user_id)
                self._delete_namespace(user_id)
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

                for ws in workspaces:
                    try:
                        await self._reconcile_workspace(ws)
                    except Exception as e:
                        logger.error("Reconcile error for %s: %s", ws.workspace_id, e)

            except Exception as e:
                logger.error("Reconciler poll error: %s", e)

            await asyncio.sleep(RECONCILE_INTERVAL)


# Singleton reconciler
reconciler = Reconciler()
