"""Network / egress control API tests."""

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


async def test_default_is_open(client):
    await seed_user("n-owner")
    await _login(client, "n-owner")

    resp = await client.get("/api/workspaces/ws-n-owner/network")
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "open"
    assert body["allowlist"] == []


async def test_set_offline_and_read_back(client):
    await seed_user("no-owner")
    await _login(client, "no-owner")

    resp = await client.patch(
        "/api/workspaces/ws-no-owner/network",
        json={"mode": "offline"},
    )
    assert resp.status_code == 200
    assert resp.json()["mode"] == "offline"

    fetched = await client.get("/api/workspaces/ws-no-owner/network")
    assert fetched.json()["mode"] == "offline"


async def test_set_allowlist_with_cidr_and_host(client):
    await seed_user("na-owner")
    await _login(client, "na-owner")

    resp = await client.patch(
        "/api/workspaces/ws-na-owner/network",
        json={"mode": "allowlist", "allowlist": ["10.20.0.0/16", "pypi.org"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "allowlist"
    assert body["allowlist"] == ["10.20.0.0/16", "pypi.org"]


async def test_invalid_allowlist_rejected(client):
    await seed_user("ni-owner")
    await _login(client, "ni-owner")

    bad = await client.patch(
        "/api/workspaces/ws-ni-owner/network",
        json={"mode": "allowlist", "allowlist": ["http://evil.example.com/path"]},
    )
    assert bad.status_code == 400

    bad_mode = await client.patch(
        "/api/workspaces/ws-ni-owner/network",
        json={"mode": "squirrel"},
    )
    assert bad_mode.status_code == 422  # pydantic pattern


async def test_get_requires_view(client):
    await seed_user("nv-owner")
    await seed_user("nv-viewer")
    await _login(client, "nv-owner")
    group_id = await _share_with(client, "nv-viewers", "ws-nv-owner", "view")
    await client.post(f"/api/groups/{group_id}/members", json={"username": "nv-viewer"})

    await _login(client, "nv-viewer")
    assert (await client.get("/api/workspaces/ws-nv-owner/network")).status_code == 200
    assert (await client.patch("/api/workspaces/ws-nv-owner/network", json={"mode": "offline"})).status_code == 403


async def test_patch_requires_operate(client):
    await seed_user("np-owner")
    await seed_user("np-stranger")
    await _login(client, "np-owner")
    await _share_with(client, "np-closed", "ws-np-owner", "operate")  # stranger not a member

    await _login(client, "np-stranger")
    assert (await client.get("/api/workspaces/ws-np-owner/network")).status_code == 404
    assert (await client.patch("/api/workspaces/ws-np-owner/network", json={"mode": "offline"})).status_code == 404


async def test_member_with_operate_can_change(client):
    await seed_user("nw-owner")
    await seed_user("nw-member")
    await _login(client, "nw-owner")
    group_id = await _share_with(client, "nw-ops", "ws-nw-owner", "operate")
    await client.post(f"/api/groups/{group_id}/members", json={"username": "nw-member"})

    await _login(client, "nw-member")
    resp = await client.patch("/api/workspaces/ws-nw-owner/network", json={"mode": "offline"})
    assert resp.status_code == 200
    assert resp.json()["mode"] == "offline"
