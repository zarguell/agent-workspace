"""Reconciler state-machine tests with fake K8s clients and a real DB.

The reconciler's K8s surface (_init_k8s) is replaced with recording fakes;
_check_pod_ready is monkeypatched to a controllable result. Everything else
(the ORM writes, canvas-key backfill, state transitions) runs against the
real test Postgres.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Optional

import pytest
import pytest_asyncio
from kubernetes.client.rest import ApiException

from conftest import seed_user


@pytest_asyncio.fixture(autouse=True)
async def _clear_starting_workspaces(db):
    """The concurrent-start cap counts every 'starting' workspace in the
    shared test DB, and tests accumulate leftovers between runs. Reset them
    before each test so the default cap (4) can never starve an unrelated
    test's start transition."""
    from sqlalchemy import update
    from models import Workspace
    await db.execute(
        update(Workspace).where(Workspace.state == "starting").values(state="hibernated")
    )
    await db.commit()
    yield


# ─── Fake K8s clients ───────────────────────────────────────────────────

class FakeAppsV1Api:
    """Tracks deployment create/scale calls; read 404s until created."""

    def __init__(self):
        self.created = False
        self.created_body = None
        self.patch_replicas: list = []
        self.deployment_exists = False
        self.deleted: list[tuple] = []

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
        self.deleted.append(("delete_namespaced_deployment", name, namespace))
        if not self.deployment_exists:
            raise ApiException(status=404)
        self.deployment_exists = False



class FakeCoreV1Api:
    """Any read_* 404s (resource absent → reconciler creates); create_* recorded.

    Namespace lifecycle is stateful: `namespace_phase` is None (absent) until
    created (Active), then Terminating once delete_namespace is issued, then
    None again when the test simulates the namespace finally going away.
    """

    def __init__(self):
        self.created: list[tuple] = []
        self.deleted: list[tuple] = []
        self.namespace_phase: Optional[str] = None

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

    def read_namespace(self, name):
        if self.namespace_phase is None:
            raise ApiException(status=404)
        return SimpleNamespace(status=SimpleNamespace(phase=self.namespace_phase))

    def create_namespace(self, body):
        self.created.append(("create_namespace", (body,), {}))
        self.namespace_phase = "Active"

    def delete_namespace(self, name):
        self.deleted.append(("delete_namespace", name))
        if self.namespace_phase is not None:
            self.namespace_phase = "Terminating"

    def delete_namespaced_service(self, name, namespace):
        self.deleted.append(("delete_namespaced_service", name, namespace))

    def delete_namespaced_resource_quota(self, name, namespace):
        self.deleted.append(("delete_namespaced_resource_quota", name, namespace))

    def delete_namespaced_service_account(self, name, namespace):
        self.deleted.append(("delete_namespaced_service_account", name, namespace))

class FakeNetworkingV1Api(FakeCoreV1Api):
    """Stateful for network policies: read returns the stored policy once
    created (so mode changes exercise the patch path)."""

    def __init__(self):
        super().__init__()
        self._policy = None

    def read_namespaced_network_policy(self, name, namespace):
        if self._policy is None:
            raise ApiException(status=404)
        return self._policy

    def create_namespaced_network_policy(self, namespace, body):
        self._policy = body
        self.created.append(("create_namespaced_network_policy", (namespace, body), {}))

    def patch_namespaced_network_policy(self, name, namespace, body):
        self._policy = body
        self.created.append(("patch_namespaced_network_policy", (name, namespace, body), {}))

    def delete_namespaced_network_policy(self, name, namespace):
        self.deleted.append(("delete_namespaced_network_policy", name, namespace))
        self._policy = None
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
        "INSERT INTO workspaces (workspace_id, user_id, state, image, idle_timeout_minutes, network_mode, egress_allowlist, created_at) "
        "VALUES (:id, :uid, 'starting', '', 15, 'open', '[]'::jsonb, now())"
    ), {"id": ws_id, "uid": str(user.user_id)})
    await db.commit()

    ready = {"ready": False}
    rec, apps, _, _ = make_reconciler(monkeypatch, ready)
    await rec._reconcile_workspace(await _fresh_workspace(db, ws_id))

    from secrets_store import SECRETS_STABLE, decrypt_value

    fresh = await _fresh_workspace(db, ws_id)
    assert fresh.canvas_api_key
    assert fresh.canvas_secret_key
    assert SECRETS_STABLE  # conftest pins SECRETS_MASTER_KEY for the suite
    # At rest the backfilled values are Fernet tokens, not plaintext — DB
    # readers and backup dumps can't see the keys (audit M5).
    assert fresh.canvas_api_key.startswith("gAAAAA")
    assert fresh.canvas_secret_key.startswith("gAAAAA")
    assert len(decrypt_value(fresh.canvas_api_key)) == 64  # token_hex(32)
    assert len(decrypt_value(fresh.canvas_secret_key)) == 64
    assert fresh.agent_token  # backfilled alongside the canvas keys
    assert len(fresh.agent_token) == 43  # secrets.token_urlsafe(32)

    env = _deploy_env(apps)
    # The pod receives the DECRYPTED plaintext keys, never the ciphertext.
    assert env["LOCAL_BACKEND_API_KEY"] == decrypt_value(fresh.canvas_api_key)
    assert env["OH_SECRET_KEY"] == decrypt_value(fresh.canvas_secret_key)
    # The pod authenticates usage reports with its own per-workspace token;
    # the shared gateway secret is no longer injected into pods.
    assert env["WORKSPACE_AGENT_TOKEN"] == fresh.agent_token
    assert "SERVICE_AUTH_TOKEN" not in env


async def test_canvas_keys_legacy_plaintext_injected_verbatim(db, monkeypatch):
    """Pre-encryption rows store plaintext canvas keys; they must be
    injected as-is (no Fernet prefix -> no decrypt attempt, no rewrite)."""
    user = await seed_user("legacy-plain", create_workspace=False)
    ws_id = "ws-legacy-plain"
    from sqlalchemy import text
    await db.execute(text(
        "INSERT INTO workspaces (workspace_id, user_id, state, image, idle_timeout_minutes, network_mode, egress_allowlist, canvas_api_key, canvas_secret_key, created_at) "
        "VALUES (:id, :uid, 'starting', '', 15, 'open', '[]'::jsonb, :api, :sec, now())"
    ), {"id": ws_id, "uid": str(user.user_id), "api": "legacy-api-key-0001", "sec": "legacy-secret-key-0001"})
    await db.commit()

    ready = {"ready": False}
    rec, apps, _, _ = make_reconciler(monkeypatch, ready)
    await rec._reconcile_workspace(await _fresh_workspace(db, ws_id))

    # Legacy row untouched (no re-encryption) and injected verbatim.
    fresh = await _fresh_workspace(db, ws_id)
    assert fresh.canvas_api_key == "legacy-api-key-0001"
    assert fresh.canvas_secret_key == "legacy-secret-key-0001"
    env = _deploy_env(apps)
    assert env["LOCAL_BACKEND_API_KEY"] == "legacy-api-key-0001"
    assert env["OH_SECRET_KEY"] == "legacy-secret-key-0001"


def test_canvas_key_marker_decrypt_unit():
    """decrypt_value_if_encrypted: Fernet tokens (gAAAAA prefix) decrypt to
    plaintext, legacy values pass through unchanged, and an undecryptable
    token raises instead of leaking ciphertext."""
    from secrets_store import SECRETS_STABLE, SecretDecryptionError, decrypt_value_if_encrypted, encrypt_value

    assert SECRETS_STABLE  # conftest pins SECRETS_MASTER_KEY for the suite

    token = encrypt_value("plaintext-credential")
    assert token.startswith("gAAAAA")
    assert decrypt_value_if_encrypted(token) == "plaintext-credential"
    # Legacy plaintext (pre-encryption rows) passes through unchanged.
    assert decrypt_value_if_encrypted("legacy-plaintext") == "legacy-plaintext"
    assert decrypt_value_if_encrypted("") == ""
    # A token that cannot be decrypted must raise loudly, never return it.
    tampered = token[:8] + ("A" if token[8] != "A" else "B") + token[9:]
    with pytest.raises(SecretDecryptionError):
        decrypt_value_if_encrypted(tampered)


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


async def test_agent_token_generated_persisted_and_injected(db, monkeypatch):
    user = await seed_user("tok-user", create_workspace=False)
    ws = await _insert_workspace(db, user, "ws-tok-user", "starting")

    ready = {"ready": False}
    rec, apps, _, _ = make_reconciler(monkeypatch, ready)
    await rec._reconcile_workspace(ws)

    fresh = await _fresh_workspace(db, "ws-tok-user")
    assert fresh.agent_token
    assert len(fresh.agent_token) == 43  # secrets.token_urlsafe(32)

    env = _deploy_env(apps)
    assert env["WORKSPACE_AGENT_TOKEN"] == fresh.agent_token
    assert env["CONTROL_PLANE_URL"].startswith("http://control-plane")
    assert "SERVICE_AUTH_TOKEN" not in env


async def test_agent_token_stable_across_reconciles(db, monkeypatch):
    user = await seed_user("tok-stable", create_workspace=False)
    ws = await _insert_workspace(db, user, "ws-tok-stable", "starting")

    ready = {"ready": False}
    rec, apps, _, _ = make_reconciler(monkeypatch, ready)
    await rec._reconcile_workspace(ws)
    first = await _fresh_workspace(db, "ws-tok-stable")

    await rec._reconcile_workspace(first)
    second = await _fresh_workspace(db, "ws-tok-stable")
    assert second.agent_token == first.agent_token
    assert apps.patch_replicas == []  # deployment already present: no churn


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


async def test_running_recreates_deleted_deployment(db, monkeypatch):
    user = await seed_user("h4-user", create_workspace=False)
    ws = await _insert_workspace(db, user, "ws-h4-user", "running")

    # Pod unreachable AND the Deployment is gone (e.g. kubectl delete
    # deployment while running): the workspace must recover instead of
    # bricking in "running" forever.
    ready = {"ready": False}
    rec, apps, _, _ = make_reconciler(monkeypatch, ready)
    assert apps.deployment_exists is False

    await rec._reconcile_workspace(ws)

    assert apps.created is True  # Deployment recreated via the ensure path
    fresh = await _fresh_workspace(db, "ws-h4-user")
    assert fresh.state == "running"  # no state regression
    env = _deploy_env(apps)
    assert env["WORKSPACE_AGENT_TOKEN"] == fresh.agent_token


async def test_hibernating_recreates_missing_deployment(db, monkeypatch):
    user = await seed_user("h4-hib", create_workspace=False)
    ws = await _insert_workspace(db, user, "ws-h4-hib", "hibernating")

    ready = {"ready": False}
    rec, apps, _, _ = make_reconciler(monkeypatch, ready)
    await rec._reconcile_workspace(ws)

    # Deployment was deleted before the scale-down pass: hibernation
    # recreates it (so the workspace stays resumable) then scales to 0.
    assert apps.created is True
    assert apps.patch_replicas == [0]
    assert (await _fresh_workspace(db, "ws-h4-hib")).state == "hibernated"


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
    assert env["WORKSPACE_ID"] == "ws-sec-user"
    assert env["USERNAME"] == "sec-user"


async def test_network_policy_open_shape(db, monkeypatch):
    rec, _, _, net = make_reconciler(monkeypatch, {"ready": False})
    rec._init_k8s()
    rec._ensure_network_policy("user-id-0001", mode="open", allowlist=None)

    created = [c for c in net.created if c[0] == "create_namespaced_network_policy"]
    assert len(created) == 1
    spec = created[0][1][1].to_dict()["spec"]
    assert spec["policy_types"] == ["Ingress"]
    assert spec["egress"] is None  # egress unrestricted
    # Ingress is locked down: gateway (all ports) + control-plane (:9000 probe).
    ingress = spec["ingress"]
    assert len(ingress) == 2
    gw = ingress[0]["_from"][0]
    assert gw["namespace_selector"]["match_labels"] == {"kubernetes.io/metadata.name": "agent-platform"}
    assert gw["pod_selector"]["match_labels"] == {"app.kubernetes.io/name": "gateway"}
    assert not ingress[0]["ports"]  # gateway reaches the full proxy surface
    cp = ingress[1]
    assert cp["_from"][0]["pod_selector"]["match_labels"] == {"app.kubernetes.io/name": "control-plane"}
    assert cp["ports"][0]["port"] == 9000
    assert cp["ports"][0]["protocol"] == "TCP"


async def test_network_policy_offline_denies_egress(db, monkeypatch):
    rec, _, _, net = make_reconciler(monkeypatch, {"ready": False})
    rec._init_k8s()
    rec._ensure_network_policy("user-id-0002", mode="offline", allowlist=None)

    created = [c for c in net.created if c[0] == "create_namespaced_network_policy"]
    spec = created[0][1][1].to_dict()["spec"]
    assert spec["policy_types"] == ["Ingress", "Egress"]
    assert spec["egress"] == []  # deny ALL egress


async def test_network_policy_allowlist_shape(db, monkeypatch):
    rec, _, _, net = make_reconciler(monkeypatch, {"ready": False})
    rec._init_k8s()

    # Deterministic hostname resolution for the allowlist entry.
    from reconciler import socket as rec_socket
    monkeypatch.setattr(rec_socket, "gethostbyname_ex", lambda h: (h, [], ["93.184.216.34"]))
    monkeypatch.setattr(rec_socket, "gethostbyname", lambda h: "93.184.216.34")

    rec._ensure_network_policy("user-id-0003", mode="allowlist", allowlist=["10.20.0.0/16", "pypi.org"])

    created = [c for c in net.created if c[0] == "create_namespaced_network_policy"]
    spec = created[0][1][1].to_dict()["spec"]
    assert spec["policy_types"] == ["Ingress", "Egress"]

    egress = spec["egress"]
    assert egress[0]["to"][0]["pod_selector"]["match_labels"] == {"k8s-app": "kube-dns"}  # DNS
    assert egress[0]["ports"][0]["port"] == 53
    # platform namespace rule
    assert egress[1]["to"][0]["namespace_selector"]["match_labels"] == {"kubernetes.io/metadata.name": "agent-platform"}
    # explicit CIDR
    assert any((e["to"][0].get("ip_block") or {}).get("cidr") == "10.20.0.0/16" for e in egress)
    # resolved hostname IP
    assert any((e["to"][0].get("ip_block") or {}).get("cidr") == "93.184.216.34/32" for e in egress)


async def test_network_policy_patched_when_mode_changes(db, monkeypatch):
    rec, _, _, net = make_reconciler(monkeypatch, {"ready": False})
    rec._init_k8s()

    # First pass creates; second pass with a different mode patches.
    rec._ensure_network_policy("user-id-0004", mode="open", allowlist=None)
    rec._ensure_network_policy("user-id-0004", mode="offline", allowlist=None)

    created = [c for c in net.created if c[0] == "create_namespaced_network_policy"]
    patched = [c for c in net.created if c[0] == "patch_namespaced_network_policy"]
    assert len(created) == 1
    assert len(patched) == 1

# ─── Wave 4: live idle, start deadline, deletion ────────────────────────

async def test_idle_pending_returns_to_running_on_fresh_activity(db, monkeypatch):
    """Fresh activity during idle_pending cancels the pending hibernate."""
    user = await seed_user("idle-fresh", create_workspace=False)
    ws = await _insert_workspace(
        db, user, "ws-idle-fresh", "idle_pending",
        last_activity_at=datetime.now(timezone.utc) - timedelta(seconds=5),
        idle_timeout_minutes=15,
    )
    rec, _, _, _ = make_reconciler(monkeypatch, {"ready": True})
    await rec._reconcile_workspace(ws)
    assert (await _fresh_workspace(db, "ws-idle-fresh")).state == "running"


async def test_idle_pending_stays_pending_in_grace_window(db, monkeypatch):
    """Past the idle timeout but inside the grace window: stay idle_pending
    (no running↔idle_pending oscillation)."""
    user = await seed_user("idle-grace", create_workspace=False)
    ws = await _insert_workspace(
        db, user, "ws-idle-grace", "idle_pending",
        # 15m10s idle: 15m timeout + 30s grace → inside the window.
        last_activity_at=datetime.now(timezone.utc) - timedelta(minutes=15, seconds=10),
        idle_timeout_minutes=15,
    )
    rec, _, _, _ = make_reconciler(monkeypatch, {"ready": True})
    await rec._reconcile_workspace(ws)
    assert (await _fresh_workspace(db, "ws-idle-grace")).state == "idle_pending"


async def test_starting_times_out_to_failed(db, monkeypatch):
    """A workspace stuck in starting past START_TIMEOUT_MINUTES is failed."""
    user = await seed_user("slow-start", create_workspace=False)
    ws = await _insert_workspace(
        db, user, "ws-slow-start", "starting",
        started_at=datetime.now(timezone.utc) - timedelta(minutes=15),
    )
    rec, apps, _, _ = make_reconciler(monkeypatch, {"ready": False})
    await rec._reconcile_workspace(ws)
    fresh = await _fresh_workspace(db, "ws-slow-start")
    assert fresh.state == "failed"
    assert apps.created is False  # no K8s work for a timed-out start


async def test_starting_without_started_at_treated_fresh(db, monkeypatch):
    """A NULL started_at never trips the deadline (treated as fresh)."""
    user = await seed_user("no-started", create_workspace=False)
    ws = await _insert_workspace(db, user, "ws-no-started", "starting")
    rec, _, _, _ = make_reconciler(monkeypatch, {"ready": False})
    await rec._reconcile_workspace(ws)
    assert (await _fresh_workspace(db, "ws-no-started")).state == "starting"


async def test_delete_preserve_pvc_keeps_namespace_and_hibernates(db, monkeypatch):
    """preserve_pvc delete: pod-facing resources removed, namespace + PVC
    kept, state=hibernated (restartable, data intact)."""
    user = await seed_user("preserve", create_workspace=False)
    ws = await _insert_workspace(db, user, "ws-preserve", "deleting")
    # Set on the ORM instance directly: the admin handler plumbing
    # (main.py) will persist this column; the reconciler reads it
    # defensively either way.
    ws.preserve_pvc = True

    rec, apps, core, net = make_reconciler(monkeypatch, {"ready": False})
    rec._init_k8s()
    core.namespace_phase = "Active"  # namespace exists with data

    await rec._reconcile_workspace(ws)

    fresh = await _fresh_workspace(db, "ws-preserve")
    assert fresh.state == "hibernated"

    deleted = {d[0] for d in apps.deleted + core.deleted + net.deleted}
    assert {"delete_namespaced_deployment", "delete_namespaced_service",
            "delete_namespaced_network_policy", "delete_namespaced_resource_quota",
            "delete_namespaced_service_account"} <= deleted
    # Namespace and PVC survive — data intact, restartable.
    assert "delete_namespace" not in deleted
    assert "delete_namespaced_persistent_volume_claim" not in deleted
    assert core.namespace_phase == "Active"


async def test_delete_marks_deleted_only_after_namespace_404(db, monkeypatch):
    """Full teardown keeps state=deleting until the namespace reads 404."""
    user = await seed_user("del-wait", create_workspace=False)
    ws = await _insert_workspace(db, user, "ws-del-wait", "deleting")
    rec, _, core, _ = make_reconciler(monkeypatch, {"ready": False})
    rec._init_k8s()
    core.namespace_phase = "Active"

    # Pass 1: deletion issued, namespace still exists → stays deleting.
    await rec._reconcile_workspace(ws)
    assert (await _fresh_workspace(db, "ws-del-wait")).state == "deleting"
    assert any(d[0] == "delete_namespace" for d in core.deleted)
    assert core.namespace_phase == "Terminating"

    # Pass 2: namespace still Terminating → still deleting.
    await rec._reconcile_workspace(await _fresh_workspace(db, "ws-del-wait"))
    assert (await _fresh_workspace(db, "ws-del-wait")).state == "deleting"

    # Pass 3: namespace finally gone (read 404s) → deleted.
    core.namespace_phase = None
    await rec._reconcile_workspace(await _fresh_workspace(db, "ws-del-wait"))
    assert (await _fresh_workspace(db, "ws-del-wait")).state == "deleted"


async def test_delete_stuck_terminating_logs_loudly_and_keeps_deleting(db, monkeypatch, caplog):
    """A namespace stuck Terminating past DELETE_TIMEOUT_MINUTES logs loudly
    and stays deleting — never claims deleted while the namespace exists."""
    import logging
    user = await seed_user("del-stuck", create_workspace=False)
    ws = await _insert_workspace(db, user, "ws-del-stuck", "deleting")
    rec, _, core, _ = make_reconciler(monkeypatch, {"ready": False})
    rec._init_k8s()
    core.namespace_phase = "Active"
    await rec._reconcile_workspace(ws)
    assert (await _fresh_workspace(db, "ws-del-stuck")).state == "deleting"

    # Simulate the namespace stuck well past DELETE_TIMEOUT_MINUTES.
    rec._ns_delete_started[ws.user_id] = datetime.now(timezone.utc) - timedelta(minutes=30)
    with caplog.at_level(logging.ERROR, logger="control-plane.reconciler"):
        await rec._reconcile_workspace(await _fresh_workspace(db, "ws-del-stuck"))
    assert (await _fresh_workspace(db, "ws-del-stuck")).state == "deleting"
    assert "stuck Terminating" in caplog.text


async def test_ensure_namespace_skips_terminating_namespace(db, monkeypatch):
    """Resources are not created into a Terminating (dying) namespace."""
    user = await seed_user("term-user", create_workspace=False)
    ws = await _insert_workspace(db, user, "ws-term-user", "starting")
    rec, apps, core, _ = make_reconciler(monkeypatch, {"ready": False})
    core.namespace_phase = "Terminating"

    await rec._reconcile_workspace(ws)

    assert rec._ensure_namespace(user.user_id) is False
    assert apps.created is False
    assert core.created == []
    # State unchanged: retried on the next pass.
    assert (await _fresh_workspace(db, "ws-term-user")).state == "starting"


# ─── Final hardening: start capacity, netpol determinism, backoff ──────


async def test_requested_defers_at_starting_capacity(db, monkeypatch):
    """requested→starting honours MAX_CONCURRENT_STARTS: at capacity the
    workspace stays requested and NO pod resources are created; it
    transitions once a slot frees."""
    import reconciler as rec_module
    user = await seed_user("cap-user", create_workspace=False)
    await _insert_workspace(db, user, "ws-cap-user", "requested")
    # A blocker workspace occupying one starting slot.
    blocker = await seed_user("cap-blocker", create_workspace=False)
    await _insert_workspace(db, blocker, "ws-cap-blocker", "starting")

    from sqlalchemy import func, select, update
    from models import Workspace as WS
    used = (await db.execute(
        select(func.count()).select_from(WS).where(WS.state == "starting")
    )).scalar_one()
    # Cap exactly the current number of starting workspaces → no free slot.
    monkeypatch.setattr(rec_module, "MAX_CONCURRENT_STARTS", used)

    rec, apps, core, _ = make_reconciler(monkeypatch, {"ready": False})
    await rec._reconcile_workspace(await _fresh_workspace(db, "ws-cap-user"))

    fresh = await _fresh_workspace(db, "ws-cap-user")
    assert fresh.state == "requested"  # deferred, not transitioned
    assert apps.created is False
    assert core.created == []  # no pod resources created this pass

    # Free a slot → the next pass transitions.
    await db.execute(
        update(WS).where(WS.workspace_id == "ws-cap-blocker").values(state="hibernated")
    )
    await db.commit()
    await rec._reconcile_workspace(await _fresh_workspace(db, "ws-cap-user"))
    assert (await _fresh_workspace(db, "ws-cap-user")).state == "starting"


async def test_network_policy_allowlist_builds_identical_specs(db, monkeypatch):
    """Hostname→IP resolution produces a set; rules must be sorted+deduped so
    two builds with the same allowlist yield byte-identical specs (no
    spurious patch on every reconcile)."""
    from reconciler import socket as rec_socket
    monkeypatch.setattr(rec_socket, "gethostbyname_ex",
                        lambda h: (h, [], ["10.0.0.3", "10.0.0.1", "10.0.0.2"]))
    monkeypatch.setattr(rec_socket, "gethostbyname", lambda h: "10.0.0.1")

    rec, _, _, net = make_reconciler(monkeypatch, {"ready": False})
    rec._init_k8s()
    rec._ensure_network_policy("user-id-0005", mode="allowlist", allowlist=["pypi.org"])
    first = net._policy.to_dict()
    # Simulate a fresh build (e.g. after a reconciler restart).
    net._policy = None
    rec._ensure_network_policy("user-id-0005", mode="allowlist", allowlist=["pypi.org"])
    second = net._policy.to_dict()

    assert first == second  # identical specs across builds
    ip_cidrs = [
        e["to"][0]["ip_block"]["cidr"]
        for e in first["spec"]["egress"]
        if (e["to"][0].get("ip_block") or {}).get("cidr")
    ]
    # Sorted and deduped: 10.0.0.1 appears once (it came from both the
    # gethostbyname_ex list and gethostbyname), and the trio is ordered.
    assert ip_cidrs == ["10.0.0.1/32", "10.0.0.2/32", "10.0.0.3/32"]


async def test_backoff_skips_failing_workspace_then_retries(db, monkeypatch):
    """A workspace whose reconcile raises is skipped for the backoff window;
    once the window elapses it is retried, and a clean pass resets it."""
    import reconciler as rec_module
    monkeypatch.setattr(rec_module, "RECONCILE_BACKOFF", {})  # isolate
    user = await seed_user("bk-user", create_workspace=False)
    ws = await _insert_workspace(db, user, "ws-bk-user", "requested")

    rec, apps, _, _ = make_reconciler(monkeypatch, {"ready": False})
    real_reconcile = rec._reconcile_workspace
    calls = {"n": 0}

    async def flaky(ws_arg):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("k8s backend down")
        await real_reconcile(ws_arg)

    monkeypatch.setattr(rec, "_reconcile_workspace", flaky)

    # Pass 1: unhandled exception → failure recorded (backoff now active).
    await rec._reconcile_with_backoff(ws)
    failures, _ = rec_module.RECONCILE_BACKOFF[user.user_id]
    assert failures == 1
    assert (await _fresh_workspace(db, "ws-bk-user")).state == "requested"

    # Pass 2: still inside the window (30s · 2^1 = 60s) → skipped entirely;
    # the failure count is neither incremented nor reset by the skip.
    await rec._reconcile_with_backoff(ws)
    assert calls["n"] == 1
    assert rec_module.RECONCILE_BACKOFF[user.user_id][0] == 1

    # Window elapses → retried; the clean pass clears the backoff entry.
    failures, _ = rec_module.RECONCILE_BACKOFF[user.user_id]
    rec_module.RECONCILE_BACKOFF[user.user_id] = (
        failures, datetime.now(timezone.utc) - timedelta(seconds=10_000),
    )
    await rec._reconcile_with_backoff(ws)
    assert calls["n"] == 2
    assert user.user_id not in rec_module.RECONCILE_BACKOFF
    assert (await _fresh_workspace(db, "ws-bk-user")).state == "starting"


async def test_backoff_benign_early_return_is_clean(db, monkeypatch):
    """A benign early return (Terminating namespace skip) is a clean pass:
    it must not count as a failure and clears any recorded failures."""
    import reconciler as rec_module
    monkeypatch.setattr(rec_module, "RECONCILE_BACKOFF", {})  # isolate
    user = await seed_user("bk-term", create_workspace=False)
    ws = await _insert_workspace(db, user, "ws-bk-term", "starting")
    rec, apps, core, _ = make_reconciler(monkeypatch, {"ready": False})
    core.namespace_phase = "Terminating"
    # Pretend this workspace had failed several times before, with the
    # backoff window already elapsed so this pass actually runs.
    rec_module.RECONCILE_BACKOFF[user.user_id] = (
        3, datetime.now(timezone.utc) - timedelta(seconds=10_000),
    )

    await rec._reconcile_with_backoff(ws)

    assert user.user_id not in rec_module.RECONCILE_BACKOFF  # reset, not failed
    assert apps.created is False
    assert core.created == []
    assert (await _fresh_workspace(db, "ws-bk-term")).state == "starting"
