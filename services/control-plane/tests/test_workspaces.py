"""Workspace API tests: listing, ownership, idempotent start, hibernate.

Tests that mutate workspace state use their own user: login auto-creates
the workspace and previous tests leave it in non-"requested" states, so a
shared fixture user would contaminate assertions.
"""

from conftest import seed_user


async def _login(client, username, password="pw"):
    return await client.post("/api/login", json={"username": username, "password": password})


async def test_list_workspaces_returns_only_own(client):
    await seed_user("alice", is_admin=True)
    await seed_user("bob")
    await _login(client, "bob")

    resp = await client.get("/api/workspaces")
    assert resp.status_code == 200
    workspaces = resp.json()
    assert len(workspaces) == 1
    assert workspaces[0]["workspace_id"] == "ws-bob"


async def test_get_own_workspace(client):
    await seed_user("alice")
    await _login(client, "alice")

    resp = await client.get("/api/workspaces/ws-alice")
    assert resp.status_code == 200
    body = resp.json()
    assert body["workspace_id"] == "ws-alice"
    assert body["username"] == "alice"


async def test_get_other_users_workspace_forbidden(client):
    await seed_user("alice")
    await seed_user("bob")
    await _login(client, "bob")

    resp = await client.get("/api/workspaces/ws-alice")
    assert resp.status_code == 404


async def test_get_workspace_admin_can_read_any(client):
    await seed_user("alice", is_admin=True)
    await seed_user("bob")
    await _login(client, "alice")

    resp = await client.get("/api/workspaces/ws-bob")
    assert resp.status_code == 200
    assert resp.json()["workspace_id"] == "ws-bob"


async def test_get_unknown_workspace(client):
    await seed_user("alice")
    await _login(client, "alice")

    resp = await client.get("/api/workspaces/ws-nope")
    assert resp.status_code == 404


async def test_start_idempotent_same_key_same_body(client):
    await seed_user("idem-user")
    await _login(client, "idem-user")

    first = await client.post(
        "/api/workspaces/ws-idem-user/start",
        headers={"Idempotency-Key": "key-1"},
    )
    second = await client.post(
        "/api/workspaces/ws-idem-user/start",
        headers={"Idempotency-Key": "key-1"},
    )
    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json() == second.json()
    assert second.json()["state"] == "starting"


async def test_start_conflict_same_key_different_body(client):
    await seed_user("conflict-user")
    await _login(client, "conflict-user")

    first = await client.post(
        "/api/workspaces/ws-conflict-user/start",
        headers={"Idempotency-Key": "key-2"},
        json={"foo": 1},
    )
    second = await client.post(
        "/api/workspaces/ws-conflict-user/start",
        headers={"Idempotency-Key": "key-2"},
        json={"foo": 2},
    )
    assert first.status_code == 202
    assert second.status_code == 409
    assert second.json()["error"] == "Idempotency key conflict: different request body"


async def test_start_noop_when_already_starting(client):
    await seed_user("noop-user")
    await _login(client, "noop-user")

    first = await client.post("/api/workspaces/ws-noop-user/start")
    second = await client.post("/api/workspaces/ws-noop-user/start")
    assert first.status_code == 202
    assert second.status_code == 200  # already starting → current state returned
    assert second.json()["state"] == "starting"


async def test_hibernate_transitions_to_hibernating(client, db):
    await seed_user("hib-ok")
    await _login(client, "hib-ok")

    # Hibernate requires a hibernate-able state; drive DB state directly.
    from sqlalchemy import update
    from models import Workspace
    await db.execute(
        update(Workspace).where(Workspace.workspace_id == "ws-hib-ok").values(state="running")
    )
    await db.commit()

    resp = await client.post("/api/workspaces/ws-hib-ok/hibernate")
    assert resp.status_code == 202
    assert resp.json()["state"] == "hibernating"

    status = await client.get("/api/workspaces/ws-hib-ok")
    assert status.json()["state"] == "hibernating"


async def test_hibernate_rejected_from_requested(client):
    await seed_user("hib-reject")
    await _login(client, "hib-reject")

    resp = await client.post("/api/workspaces/ws-hib-reject/hibernate")
    assert resp.status_code == 409
