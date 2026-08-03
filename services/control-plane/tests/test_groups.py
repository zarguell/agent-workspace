"""Group CRUD + membership authz tests."""

from conftest import seed_user


async def _login(client, username, password="pw"):
    return await client.post("/api/login", json={"username": username, "password": password})


async def _create_group(client, name):
    return await client.post("/api/groups", json={"name": name})


async def test_create_group_makes_creator_admin(client):
    await seed_user("alice")
    await _login(client, "alice")

    resp = await _create_group(client, "eng")
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "eng"
    assert body["role"] == "admin"
    assert body["group_id"].startswith("grp-")

    groups = await client.get("/api/groups")
    assert len(groups.json()) == 1
    assert groups.json()[0]["name"] == "eng"


async def test_create_group_duplicate_name(client):
    await seed_user("alice")
    await _login(client, "alice")
    await _create_group(client, "dup")

    resp = await _create_group(client, "dup")
    assert resp.status_code == 409


async def test_get_group_shows_members(client):
    await seed_user("alice")
    await seed_user("bob")
    await _login(client, "alice")
    group_id = (await _create_group(client, "team")).json()["group_id"]

    resp = await client.get(f"/api/groups/{group_id}")
    assert resp.status_code == 200
    members = resp.json()["members"]
    assert len(members) == 1
    assert members[0]["username"] == "alice"
    assert members[0]["role"] == "admin"


async def test_group_hidden_from_non_members(client):
    await seed_user("alice")
    await seed_user("bob")
    await _login(client, "alice")
    group_id = (await _create_group(client, "private")).json()["group_id"]

    # Bob cannot see the group exists.
    await _login(client, "bob")
    resp = await client.get(f"/api/groups/{group_id}")
    assert resp.status_code == 404
    groups = await client.get("/api/groups")
    assert groups.json() == []


async def test_add_member_requires_group_admin(client):
    await seed_user("alice")
    await seed_user("bob")
    await seed_user("carol")
    await _login(client, "alice")
    group_id = (await _create_group(client, "admins-only")).json()["group_id"]
    add_resp = await client.post(
        f"/api/groups/{group_id}/members",
        json={"username": "bob", "role": "member"},
    )
    assert add_resp.status_code == 201

    # Bob (plain member) cannot add carol.
    await _login(client, "bob")
    resp = await client.post(
        f"/api/groups/{group_id}/members",
        json={"username": "carol"},
    )
    assert resp.status_code == 403


async def test_add_member_unknown_user(client):
    await seed_user("alice")
    await _login(client, "alice")
    group_id = (await _create_group(client, "ghost")).json()["group_id"]

    resp = await client.post(f"/api/groups/{group_id}/members", json={"username": "ghost"})
    assert resp.status_code == 404


async def test_add_duplicate_member_conflict(client):
    await seed_user("alice")
    await seed_user("bob")
    await _login(client, "alice")
    group_id = (await _create_group(client, "twice")).json()["group_id"]

    first = await client.post(f"/api/groups/{group_id}/members", json={"username": "bob"})
    second = await client.post(f"/api/groups/{group_id}/members", json={"username": "bob"})
    assert first.status_code == 201
    assert second.status_code == 409


async def test_remove_member(client):
    await seed_user("alice")
    await seed_user("bob")
    await _login(client, "alice")
    group_id = (await _create_group(client, "purge")).json()["group_id"]
    add = await client.post(f"/api/groups/{group_id}/members", json={"username": "bob"})
    bob_id = add.json()["user_id"]

    resp = await client.delete(f"/api/groups/{group_id}/members/{bob_id}")
    assert resp.status_code == 200

    detail = await client.get(f"/api/groups/{group_id}")
    usernames = [m["username"] for m in detail.json()["members"]]
    assert "bob" not in usernames


async def test_delete_group_removes_memberships(client):
    await seed_user("alice")
    await seed_user("bob")
    await _login(client, "alice")
    group_id = (await _create_group(client, "disband")).json()["group_id"]
    await client.post(f"/api/groups/{group_id}/members", json={"username": "bob"})

    resp = await client.delete(f"/api/groups/{group_id}")
    assert resp.status_code == 200

    # The deleted group is gone from alice's list…
    names = [g["name"] for g in (await client.get("/api/groups")).json()]
    assert "disband" not in names

    # …and bob's membership went with it (his other group memberships remain).
    await _login(client, "bob")
    bob_names = [g["name"] for g in (await client.get("/api/groups")).json()]
    assert "disband" not in bob_names


async def test_delete_group_requires_admin(client):
    await seed_user("alice")
    await seed_user("bob")
    await _login(client, "alice")
    group_id = (await _create_group(client, "guarded")).json()["group_id"]
    await client.post(f"/api/groups/{group_id}/members", json={"username": "bob"})

    await _login(client, "bob")
    resp = await client.delete(f"/api/groups/{group_id}")
    assert resp.status_code == 403
