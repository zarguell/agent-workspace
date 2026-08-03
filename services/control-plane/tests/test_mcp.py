"""MCP server registry CRUD + internal resolution authz."""

from conftest import seed_user


async def _login(client, username, password="pw"):
    return await client.post("/api/login", json={"username": username, "password": password})


async def _share_with(client, group_name, workspace_id, permission="operate"):
    group_id = (await client.post("/api/groups", json={"name": group_name})).json()["group_id"]
    resp = await client.post(
        f"/api/workspaces/{workspace_id}/shares",
        json={"group_id": group_id, "permission": permission},
    )
    assert resp.status_code == 201
    return group_id


async def _register(client, workspace_id, name="tools", port=3001):
    return await client.post(
        f"/api/workspaces/{workspace_id}/mcp-servers",
        json={"name": name, "port": port},
    )


async def test_register_and_list(client):
    await seed_user("m-owner")
    await _login(client, "m-owner")

    resp = await _register(client, "ws-m-owner")
    assert resp.status_code == 201
    body = resp.json()
    assert body["server_id"].startswith("mcp-")
    assert body["name"] == "tools"
    assert body["port"] == 3001
    assert body["enabled"] is True

    listed = await client.get("/api/workspaces/ws-m-owner/mcp-servers")
    assert listed.status_code == 200
    assert len(listed.json()) == 1
    assert listed.json()[0]["server_id"] == body["server_id"]


async def test_register_requires_operate(client):
    await seed_user("mo-owner")
    await seed_user("mo-viewer")
    await _login(client, "mo-owner")
    group_id = await _share_with(client, "mo-viewers", "ws-mo-owner", "view")
    await client.post(f"/api/groups/{group_id}/members", json={"username": "mo-viewer"})

    await _login(client, "mo-viewer")
    resp = await _register(client, "ws-mo-owner")
    assert resp.status_code == 403  # view is not enough to register
    listed = await client.get("/api/workspaces/ws-mo-owner/mcp-servers")
    assert listed.status_code == 200  # view CAN list


async def test_register_stranger_denied(client):
    await seed_user("ms-owner")
    await seed_user("ms-stranger")
    await _login(client, "ms-owner")
    await _share_with(client, "ms-closed", "ws-ms-owner", "operate")  # stranger not a member

    await _login(client, "ms-stranger")
    assert (await _register(client, "ws-ms-owner")).status_code == 404
    assert (await client.get("/api/workspaces/ws-ms-owner/mcp-servers")).status_code == 404


async def test_member_with_operate_can_register(client):
    await seed_user("mm-owner")
    await seed_user("mm-member")
    await _login(client, "mm-owner")
    group_id = await _share_with(client, "mm-ops", "ws-mm-owner", "operate")
    await client.post(f"/api/groups/{group_id}/members", json={"username": "mm-member"})

    await _login(client, "mm-member")
    resp = await _register(client, "ws-mm-owner")
    assert resp.status_code == 201


async def test_patch_enable_disable(client):
    await seed_user("mp-owner")
    await _login(client, "mp-owner")
    server_id = (await _register(client, "ws-mp-owner")).json()["server_id"]

    disabled = await client.patch(
        f"/api/workspaces/ws-mp-owner/mcp-servers/{server_id}",
        json={"enabled": False},
    )
    assert disabled.status_code == 200
    assert disabled.json()["enabled"] is False

    enabled = await client.patch(
        f"/api/workspaces/ws-mp-owner/mcp-servers/{server_id}",
        json={"enabled": True},
    )
    assert enabled.json()["enabled"] is True


async def test_delete_server(client):
    await seed_user("md-owner")
    await _login(client, "md-owner")
    server_id = (await _register(client, "ws-md-owner")).json()["server_id"]

    resp = await client.delete(f"/api/workspaces/ws-md-owner/mcp-servers/{server_id}")
    assert resp.status_code == 200
    assert (await client.get("/api/workspaces/ws-md-owner/mcp-servers")).json() == []


async def test_internal_target_owner(client, service_token):
    await seed_user("mi-owner")
    await _login(client, "mi-owner")
    server_id = (await _register(client, "ws-mi-owner")).json()["server_id"]

    resp = await client.get(
        f"/api/internal/workspaces/ws-mi-owner/mcp/{server_id}",
        headers={"X-Service-Auth": service_token, "X-Service-User": "mi-owner"},
    )
    assert resp.status_code == 200
    assert resp.json()["port"] == 3001
    assert resp.json()["enabled"] is True


async def test_internal_target_member_operate(client, service_token):
    await seed_user("mt-owner")
    await seed_user("mt-member")
    await _login(client, "mt-owner")
    server_id = (await _register(client, "ws-mt-owner")).json()["server_id"]
    group_id = await _share_with(client, "mt-ops", "ws-mt-owner", "operate")
    await client.post(f"/api/groups/{group_id}/members", json={"username": "mt-member"})

    resp = await client.get(
        f"/api/internal/workspaces/ws-mt-owner/mcp/{server_id}",
        headers={"X-Service-Auth": service_token, "X-Service-User": "mt-member"},
    )
    assert resp.status_code == 200
    assert resp.json()["workspace_id"] == "ws-mt-owner"


async def test_internal_target_stranger_denied(client, service_token):
    await seed_user("mg-owner")
    await _login(client, "mg-owner")
    server_id = (await _register(client, "ws-mg-owner")).json()["server_id"]

    resp = await client.get(
        f"/api/internal/workspaces/ws-mg-owner/mcp/{server_id}",
        headers={"X-Service-Auth": service_token, "X-Service-User": "nobody"},
    )
    assert resp.status_code == 404


async def test_internal_target_disabled_denied(client, service_token):
    await seed_user("md2-owner")
    await _login(client, "md2-owner")
    server_id = (await _register(client, "ws-md2-owner")).json()["server_id"]
    await client.patch(
        f"/api/workspaces/ws-md2-owner/mcp-servers/{server_id}",
        json={"enabled": False},
    )

    resp = await client.get(
        f"/api/internal/workspaces/ws-md2-owner/mcp/{server_id}",
        headers={"X-Service-Auth": service_token, "X-Service-User": "md2-owner"},
    )
    assert resp.status_code == 404


async def test_internal_target_unknown_server(client, service_token):
    await seed_user("mu-owner")
    await _login(client, "mu-owner")

    resp = await client.get(
        "/api/internal/workspaces/ws-mu-owner/mcp/mcp-zzz",
        headers={"X-Service-Auth": service_token, "X-Service-User": "mu-owner"},
    )
    assert resp.status_code == 404


async def test_internal_target_requires_service_user(client, service_token):
    await seed_user("mr-owner")
    await _login(client, "mr-owner")
    server_id = (await _register(client, "ws-mr-owner")).json()["server_id"]

    resp = await client.get(
        f"/api/internal/workspaces/ws-mr-owner/mcp/{server_id}",
        headers={"X-Service-Auth": service_token},
    )
    assert resp.status_code == 401
