"""Internal endpoint tests: X-Service-Auth on routing + audit ingestion.

The routing endpoint now requires X-Service-User (the gateway identifies the
end user) and grants access only to the owner, admins, or members of a group
holding operate permission — anything else is a 404 to avoid leaking
workspace existence.
"""

from conftest import seed_user


async def _login(client, username, password="pw"):
    return await client.post("/api/login", json={"username": username, "password": password})


async def _share_operate(client, group_name, workspace_id):
    group_id = (await client.post("/api/groups", json={"name": group_name})).json()["group_id"]
    resp = await client.post(
        f"/api/workspaces/{workspace_id}/shares",
        json={"group_id": group_id, "permission": "operate"},
    )
    assert resp.status_code == 201
    return group_id


async def test_routing_requires_service_auth(client):
    await seed_user("alice")
    await _login(client, "alice")

    resp = await client.get("/api/internal/workspaces/ws-alice/routing")
    assert resp.status_code == 403


async def test_routing_requires_service_user(client, service_token):
    await seed_user("alice")
    await _login(client, "alice")

    resp = await client.get(
        "/api/internal/workspaces/ws-alice/routing",
        headers={"X-Service-Auth": service_token},
    )
    assert resp.status_code == 401


async def test_routing_owner_allowed(client, service_token):
    await seed_user("alice")
    await _login(client, "alice")

    resp = await client.get(
        "/api/internal/workspaces/ws-alice/routing",
        headers={"X-Service-Auth": service_token, "X-Service-User": "alice"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["workspace_id"] == "ws-alice"
    assert body["state"] == "requested"
    assert body["cluster_ip"] is None
    assert body["agent_ready"] is False


async def test_routing_unknown_workspace(client, service_token):
    resp = await client.get(
        "/api/internal/workspaces/ws-nope/routing",
        headers={"X-Service-Auth": service_token, "X-Service-User": "alice"},
    )
    assert resp.status_code == 404


async def test_routing_unknown_user(client, service_token):
    await seed_user("alice")
    await _login(client, "alice")

    resp = await client.get(
        "/api/internal/workspaces/ws-alice/routing",
        headers={"X-Service-Auth": service_token, "X-Service-User": "ghost"},
    )
    assert resp.status_code == 404


async def test_routing_stranger_denied(client, service_token):
    await seed_user("alice")
    await seed_user("bob")
    await _login(client, "alice")

    resp = await client.get(
        "/api/internal/workspaces/ws-alice/routing",
        headers={"X-Service-Auth": service_token, "X-Service-User": "bob"},
    )
    assert resp.status_code == 404


async def test_routing_member_with_operate_allowed(client, service_token):
    # Dedicated users: the grant must not leak into other tests' alice/bob.
    await seed_user("rm-owner")
    await seed_user("rm-member")
    await _login(client, "rm-owner")
    group_id = await _share_operate(client, "rm-routers", "ws-rm-owner")
    add = await client.post(f"/api/groups/{group_id}/members", json={"username": "rm-member"})
    assert add.status_code == 201

    resp = await client.get(
        "/api/internal/workspaces/ws-rm-owner/routing",
        headers={"X-Service-Auth": service_token, "X-Service-User": "rm-member"},
    )
    assert resp.status_code == 200
    assert resp.json()["workspace_id"] == "ws-rm-owner"


async def test_routing_member_with_view_denied(client, service_token):
    await seed_user("rv-owner")
    await seed_user("rv-member")
    await _login(client, "rv-owner")
    group_id = (await client.post("/api/groups", json={"name": "rv-viewers"})).json()["group_id"]
    share = await client.post(
        "/api/workspaces/ws-rv-owner/shares",
        json={"group_id": group_id, "permission": "view"},
    )
    assert share.status_code == 201
    add = await client.post(f"/api/groups/{group_id}/members", json={"username": "rv-member"})
    assert add.status_code == 201

    resp = await client.get(
        "/api/internal/workspaces/ws-rv-owner/routing",
        headers={"X-Service-Auth": service_token, "X-Service-User": "rv-member"},
    )
    assert resp.status_code == 404  # view is not enough to route


async def test_audit_requires_service_auth(client):
    resp = await client.post("/api/audit", json={"event_type": "test"})
    assert resp.status_code == 403


async def test_audit_with_service_auth(client, service_token):
    resp = await client.post(
        "/api/audit",
        headers={"X-Service-Auth": service_token},
        json={"event_type": "gateway.route_granted", "metadata": {"route_class": "canvas"}},
    )
    assert resp.status_code == 201
    assert resp.json()["ok"] is True


async def test_routing_dev_host_mode(client, service_token, monkeypatch):
    """WORKSPACE_DEV_HOST bypasses K8s: routing returns the fixed host."""
    from unittest.mock import AsyncMock
    from reconciler import reconciler

    monkeypatch.setenv("WORKSPACE_DEV_HOST", "10.9.9.9")
    reconciler._check_pod_ready_host = AsyncMock(return_value=True)
    await seed_user("alice")
    await _login(client, "alice")

    resp = await client.get(
        "/api/internal/workspaces/ws-alice/routing",
        headers={"X-Service-Auth": service_token, "X-Service-User": "alice"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["cluster_ip"] == "10.9.9.9"
    assert body["agent_ready"] is True

async def test_routing_includes_agent_token(client, service_token):
    """The routing response carries the workspace agent token (default seed)."""
    await seed_user("tok-route")
    await _login(client, "tok-route")

    resp = await client.get(
        "/api/internal/workspaces/ws-tok-route/routing",
        headers={"X-Service-Auth": service_token, "X-Service-User": "tok-route"},
    )
    assert resp.status_code == 200
    assert resp.json()["agent_token"] == "tok-tok-route"


async def test_routing_agent_token_custom(client, service_token):
    """seed_user's agent_token param is what routing returns."""
    await seed_user("tok-custom", agent_token="custom-secret-123")
    await _login(client, "tok-custom")

    resp = await client.get(
        "/api/internal/workspaces/ws-tok-custom/routing",
        headers={"X-Service-Auth": service_token, "X-Service-User": "tok-custom"},
    )
    assert resp.status_code == 200
    assert resp.json()["agent_token"] == "custom-secret-123"


async def test_routing_agent_token_empty_for_legacy_workspace(client, service_token, db):
    """Workspaces whose agent_token is NULL (pre-column, never reconciled)
    get an empty string so the gateway refuses pod calls instead of
    sending an unauthenticated request."""
    from sqlalchemy import update
    from models import Workspace

    await seed_user("tok-legacy")
    await _login(client, "tok-legacy")
    await db.execute(
        update(Workspace).where(Workspace.workspace_id == "ws-tok-legacy").values(agent_token=None)
    )
    await db.commit()

    resp = await client.get(
        "/api/internal/workspaces/ws-tok-legacy/routing",
        headers={"X-Service-Auth": service_token, "X-Service-User": "tok-legacy"},
    )
    assert resp.status_code == 200
    assert resp.json()["agent_token"] == ""

async def test_activity_requires_service_auth(client):
    resp = await client.post("/api/internal/activity", json={"workspace_id": "ws-activity-owner"})
    assert resp.status_code == 403


async def test_activity_requires_service_user(client, service_token):
    resp = await client.post(
        "/api/internal/activity",
        headers={"X-Service-Auth": service_token},
        json={"workspace_id": "ws-activity-owner"},
    )
    assert resp.status_code == 401


async def test_activity_owner_updates_last_activity(client, service_token, db):
    await seed_user("activity-owner")
    from sqlalchemy import update
    from models import Workspace
    await db.execute(
        update(Workspace).where(Workspace.workspace_id == "ws-activity-owner").values(last_activity_at=None)
    )
    await db.commit()

    resp = await client.post(
        "/api/internal/activity",
        headers={"X-Service-Auth": service_token, "X-Service-User": "activity-owner"},
        json={"workspace_id": "ws-activity-owner"},
    )
    assert resp.status_code == 204

    ws = await db.get(Workspace, "ws-activity-owner")
    await db.refresh(ws)
    assert ws.last_activity_at is not None


async def test_activity_unknown_workspace(client, service_token):
    await seed_user("activity-owner")
    resp = await client.post(
        "/api/internal/activity",
        headers={"X-Service-Auth": service_token, "X-Service-User": "activity-owner"},
        json={"workspace_id": "ws-does-not-exist"},
    )
    assert resp.status_code == 404


async def test_activity_stranger_denied(client, service_token):
    await seed_user("activity-owner")
    await seed_user("activity-stranger")
    resp = await client.post(
        "/api/internal/activity",
        headers={"X-Service-Auth": service_token, "X-Service-User": "activity-stranger"},
        json={"workspace_id": "ws-activity-owner"},
    )
    assert resp.status_code == 404
