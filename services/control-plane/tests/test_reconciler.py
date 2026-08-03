"""Reconciler state-machine tests with fake K8s clients and a real DB.

The reconciler's K8s surface (_init_k8s) is replaced with recording fakes;
_check_pod_ready is monkeypatched to a controllable result. Everything else
(the ORM writes, canvas-key backfill, state transitions) runs against the
real test Postgres.
"""

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from kubernetes.client.rest import ApiException

from conftest import seed_user


# ─── Fake K8s clients ───────────────────────────────────────────────────

class FakeAppsV1Api:
    """Tracks deployment create/scale calls; read 404s until created."""

    def __init__(self):
        self.created = False
        self.created_body = None
        self.patch_replicas: list = []
        self.deployment_exists = False

    def read_namespaced_deployment(self, name, namespace):
        if not self.deployment_exists:
            raise ApiException(status=404)
        return SimpleNamespace(spec=SimpleNamespace(replicas=1))

    def create_namespaced_deployment(self, namespace, body):
        self.created = True
        self.created_body = body
        self.deployment_exists = True

    def patch_namespaced_deployment(self, name, namespace, body):
        if isinstance(body, dict):
            self.patch_replicas.append(body.get("spec", {}).get("replicas"))
        else:
            self.patch_replicas.append(body.spec.replicas)

    def delete_namespaced_deployment(self, name, namespace):
        self.deployment_exists = False


class FakeCoreV1Api:
    """Any read_* 404s (resource absent → reconciler creates); create_* recorded."""

    def __init__(self):
        self.created: list[tuple] = []

    def _read_404(self, *args, **kwargs):
        raise ApiException(status=404)

    def __getattr__(self, name):
        if name.startswith("read_"):
            return self._read_404
        if name.startswith("create_"):
            def _record(*args, **kwargs):
                self.created.append((name, args, kwargs))
            return _record
        raise AttributeError(name)


class FakeNetworkingV1Api(FakeCoreV1Api):
    pass


def make_reconciler(monkeypatch, ready_holder):
    """Build a Reconciler wired to fakes; returns (rec, apps, core, net)."""
    from reconciler import Reconciler
    rec = Reconciler()
    apps = FakeAppsV1Api()
    core = FakeCoreV1Api()
    net = FakeNetworkingV1Api()

    # Instance attributes hold plain functions (no implicit self binding).
    def fake_init():
        rec._k8s_apps = apps
        rec._k8s_core = core
        rec._k8s_net = net
        rec._initialized = True

    async def fake_ready(user_id):
        return ready_holder["ready"]

    monkeypatch.setattr(rec, "_init_k8s", fake_init)
    monkeypatch.setattr(rec, "_check_pod_ready", fake_ready)
    return rec, apps, core, net


# ─── Helpers ────────────────────────────────────────────────────────────

async def _fresh_workspace(db, ws_id):
    from sqlalchemy import select
    from models import Workspace
    # populate_existing: the db fixture session may already hold the row in
    # its identity map; refresh it so reconciler commits are visible.
    result = await db.execute(
        select(Workspace).where(Workspace.workspace_id == ws_id).execution_options(populate_existing=True)
    )
    return result.scalar_one()


async def _insert_workspace(db, user, ws_id, state, **kwargs):
    from models import Workspace
    db.add(Workspace(workspace_id=ws_id, user_id=user.user_id, state=state, image="", **kwargs))
    await db.commit()
    return await _fresh_workspace(db, ws_id)


def _deploy_env(apps):
    container = apps.created_body.spec.template.spec.containers[0]
    return {e.name: e.value for e in container.env}


# ─── Tests ──────────────────────────────────────────────────────────────

async def test_requested_transitions_to_starting(db, monkeypatch):
    user = await seed_user("req-user", create_workspace=False)
    ws = await _insert_workspace(db, user, "ws-req-user", "requested")

    ready = {"ready": False}
    rec, apps, _, _ = make_reconciler(monkeypatch, ready)
    await rec._reconcile_workspace(ws)

    fresh = await _fresh_workspace(db, "ws-req-user")
    assert fresh.state == "starting"
    assert fresh.image  # default image applied
    assert apps.created is False  # K8s work happens in the "starting" pass


async def test_starting_creates_resources_then_running(db, monkeypatch):
    user = await seed_user("start-user", create_workspace=False)
    ws = await _insert_workspace(db, user, "ws-start-user", "starting")

    ready = {"ready": False}
    rec, apps, core, _ = make_reconciler(monkeypatch, ready)

    await rec._reconcile_workspace(ws)
    fresh = await _fresh_workspace(db, "ws-start-user")
    assert fresh.state == "starting"  # not ready yet
    assert apps.created is True
    # Core resources (namespace, PVC, service, quota, SA, netpol) created
    create_names = {name for name, _, _ in core.created}
    assert {"create_namespace", "create_namespaced_persistent_volume_claim",
            "create_namespaced_service", "create_namespaced_resource_quota",
            "create_namespaced_service_account"} <= create_names

    ready["ready"] = True
    await rec._reconcile_workspace(await _fresh_workspace(db, "ws-start-user"))
    fresh = await _fresh_workspace(db, "ws-start-user")
    assert fresh.state == "running"
    assert fresh.started_at is not None
    assert fresh.last_activity_at is not None


async def test_canvas_keys_backfilled_and_injected_into_deployment(db, monkeypatch):
    user = await seed_user("legacy-user", create_workspace=False)
    ws_id = "ws-legacy-user"

    # Simulate a row created before the canvas-key columns existed (ALTER
    # added them as nullable): raw SQL insert leaves them NULL.
    from sqlalchemy import text
    await db.execute(text(
        "INSERT INTO workspaces (workspace_id, user_id, state, image, idle_timeout_minutes, created_at) "
        "VALUES (:id, :uid, 'starting', '', 15, now())"
    ), {"id": ws_id, "uid": str(user.user_id)})
    await db.commit()

    ready = {"ready": False}
    rec, apps, _, _ = make_reconciler(monkeypatch, ready)
    await rec._reconcile_workspace(await _fresh_workspace(db, ws_id))

    fresh = await _fresh_workspace(db, ws_id)
    assert fresh.canvas_api_key
    assert fresh.canvas_secret_key
    assert len(fresh.canvas_api_key) == 64  # token_hex(32)
    assert len(fresh.canvas_secret_key) == 64

    env = _deploy_env(apps)
    assert env["LOCAL_BACKEND_API_KEY"] == fresh.canvas_api_key
    assert env["OH_SECRET_KEY"] == fresh.canvas_secret_key
    assert env["SERVICE_AUTH_TOKEN"]  # injected so the agent can report usage


async def test_canvas_keys_stable_across_reconciles(db, monkeypatch):
    user = await seed_user("stable-user", create_workspace=False)
    ws = await _insert_workspace(db, user, "ws-stable-user", "starting")

    ready = {"ready": False}
    rec, apps, _, _ = make_reconciler(monkeypatch, ready)
    await rec._reconcile_workspace(ws)
    first = await _fresh_workspace(db, "ws-stable-user")

    await rec._reconcile_workspace(first)
    second = await _fresh_workspace(db, "ws-stable-user")
    assert second.canvas_api_key == first.canvas_api_key
    assert second.canvas_secret_key == first.canvas_secret_key


async def test_running_idle_pending_hibernating_hibernated(db, monkeypatch):
    user = await seed_user("idle-user", create_workspace=False)
    ws = await _insert_workspace(
        db, user, "ws-idle-user", "running",
        last_activity_at=datetime.now(timezone.utc) - timedelta(minutes=20),
        idle_timeout_minutes=15,
    )

    ready = {"ready": True}
    rec, apps, _, _ = make_reconciler(monkeypatch, ready)

    await rec._reconcile_workspace(ws)
    assert (await _fresh_workspace(db, "ws-idle-user")).state == "idle_pending"

    # Push last_activity past the grace period (idle + IDLE_GRACE_SECONDS)
    from sqlalchemy import update
    from models import Workspace
    await db.execute(
        update(Workspace).where(Workspace.workspace_id == "ws-idle-user").values(
            last_activity_at=datetime.now(timezone.utc) - timedelta(seconds=1000),
        )
    )
    await db.commit()

    await rec._reconcile_workspace(await _fresh_workspace(db, "ws-idle-user"))
    assert (await _fresh_workspace(db, "ws-idle-user")).state == "hibernating"

    await rec._reconcile_workspace(await _fresh_workspace(db, "ws-idle-user"))
    assert (await _fresh_workspace(db, "ws-idle-user")).state == "hibernated"
    assert apps.patch_replicas == [0]


async def test_running_stays_running_when_active(db, monkeypatch):
    user = await seed_user("active2", create_workspace=False)
    ws = await _insert_workspace(
        db, user, "ws-active2", "running",
        last_activity_at=datetime.now(timezone.utc),
        idle_timeout_minutes=15,
    )
    ready = {"ready": True}
    rec, _, _, _ = make_reconciler(monkeypatch, ready)
    await rec._reconcile_workspace(ws)
    assert (await _fresh_workspace(db, "ws-active2")).state == "running"


async def test_get_cluster_ip_resilient_without_k8s(monkeypatch):
    """Routing must not 500 when the K8s client cannot initialize."""
    from reconciler import Reconciler
    rec = Reconciler()

    def boom_init():
        raise RuntimeError("no kubeconfig")

    monkeypatch.setattr(rec, "_init_k8s", boom_init)
    assert await rec._get_cluster_ip("some-user-id") is None


async def test_secrets_injected_as_env(db, monkeypatch):
    user = await seed_user("sec-user", create_workspace=False)
    ws = await _insert_workspace(db, user, "ws-sec-user", "starting")

    from models import WorkspaceSecret
    from secrets_store import encrypt_value
    db.add(WorkspaceSecret(
        workspace_id="ws-sec-user",
        key="my_token",
        value_encrypted=encrypt_value("super-secret"),
        created_by=user.user_id,
    ))
    await db.commit()

    ready = {"ready": False}
    rec, apps, _, _ = make_reconciler(monkeypatch, ready)
    await rec._reconcile_workspace(ws)

    env = _deploy_env(apps)
    assert env["WS_SECRET_MY_TOKEN"] == "super-secret"
