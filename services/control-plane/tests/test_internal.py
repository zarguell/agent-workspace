"""Internal endpoint tests: X-Service-Auth on routing + audit ingestion."""

from conftest import seed_user


async def _login(client, username, password="pw"):
    return await client.post("/api/login", json={"username": username, "password": password})


async def test_routing_requires_service_auth(client):
    await seed_user("alice")
    await _login(client, "alice")

    resp = await client.get("/api/internal/workspaces/ws-alice/routing")
    assert resp.status_code == 403


async def test_routing_with_service_auth(client, service_token):
    await seed_user("alice")
    await _login(client, "alice")

    resp = await client.get(
        "/api/internal/workspaces/ws-alice/routing",
        headers={"X-Service-Auth": service_token},
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
        headers={"X-Service-Auth": service_token},
    )
    assert resp.status_code == 404


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
