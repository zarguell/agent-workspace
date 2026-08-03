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


async def test_idempotency_cache_never_served_across_users(client):
    """User B replaying user A's Idempotency-Key must NOT get A's cached 200.

    Regresses the disclosure where the cache was consulted before authz and
    keyed without the caller: B would have received A's cached start response.
    """
    await seed_user("idem-owner")
    await seed_user("idem-stranger")
    await _login(client, "idem-owner")

    first = await client.post(
        "/api/workspaces/ws-idem-owner/start",
        headers={"Idempotency-Key": "cross-user-key"},
    )
    assert first.status_code == 202  # A's response is now cached

    # Switch to a user with no access to A's workspace.
    await client.post("/api/logout")
    await _login(client, "idem-stranger")

    replay = await client.post(
        "/api/workspaces/ws-idem-owner/start",
        headers={"Idempotency-Key": "cross-user-key"},
    )
    assert replay.status_code in (403, 404)  # never the cached 200


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


async def _share_with(client, owner_username, group_name, workspace_id, permission="view"):
    """Create a group as owner and share the workspace with it."""
    group_id = (await client.post("/api/groups", json={"name": group_name})).json()["group_id"]
    resp = await client.post(
        f"/api/workspaces/{workspace_id}/shares",
        json={"group_id": group_id, "permission": permission},
    )
    assert resp.status_code == 201
    return group_id


async def test_share_grants_view_only(client):
    # Dedicated users: grant state must never touch the shared alice/bob.
    await seed_user("sv-owner")
    await seed_user("sv-member")
    await _login(client, "sv-owner")
    group_id = await _share_with(client, "sv-owner", "sv-viewers", "ws-sv-owner", "view")
    add = await client.post(f"/api/groups/{group_id}/members", json={"username": "sv-member"})
    assert add.status_code == 201

    # The member can read the shared workspace…
    await _login(client, "sv-member")
    resp = await client.get("/api/workspaces/ws-sv-owner")
    assert resp.status_code == 200
    assert resp.json()["workspace_id"] == "ws-sv-owner"

    # …it appears in their workspace list…
    listed = await client.get("/api/workspaces")
    assert [w["workspace_id"] for w in listed.json()] == ["ws-sv-member", "ws-sv-owner"]

    # …but view permission cannot start it.
    start = await client.post("/api/workspaces/ws-sv-owner/start")
    assert start.status_code == 403


async def test_share_operate_allows_start(client):
    await seed_user("so-owner")
    await seed_user("so-member")
    await _login(client, "so-owner")
    group_id = await _share_with(client, "so-owner", "so-operators", "ws-so-owner", "operate")
    add = await client.post(f"/api/groups/{group_id}/members", json={"username": "so-member"})
    assert add.status_code == 201

    await _login(client, "so-member")
    start = await client.post("/api/workspaces/ws-so-owner/start")
    assert start.status_code == 202
    assert start.json()["state"] == "starting"


async def test_unshare_revokes_access(client):
    await seed_user("un-owner")
    await seed_user("un-member")
    await _login(client, "un-owner")
    group_id = await _share_with(client, "un-owner", "un-temp", "ws-un-owner", "operate")
    await client.post(f"/api/groups/{group_id}/members", json={"username": "un-member"})

    unshare = await client.delete(f"/api/workspaces/ws-un-owner/shares/{group_id}")
    assert unshare.status_code == 200

    await _login(client, "un-member")
    assert (await client.get("/api/workspaces/ws-un-owner")).status_code == 404
    listed = (await client.get("/api/workspaces")).json()
    assert [w["workspace_id"] for w in listed] == ["ws-un-member"]


async def test_non_member_cannot_access(client):
    await seed_user("nm-owner")
    await seed_user("nm-member")
    await _login(client, "nm-owner")
    await _share_with(client, "nm-owner", "nm-closed", "ws-nm-owner", "operate")

    # nm-member is NOT in the group → no access despite the share existing.
    await _login(client, "nm-member")
    assert (await client.get("/api/workspaces/ws-nm-owner")).status_code == 404


async def test_share_requires_owner(client):
    await seed_user("hr-owner")
    await seed_user("hr-member")
    await _login(client, "hr-owner")
    group_id = (await client.post("/api/groups", json={"name": "hr-hijack"})).json()["group_id"]

    await _login(client, "hr-member")
    resp = await client.post(
        "/api/workspaces/ws-hr-owner/shares",
        json={"group_id": group_id, "permission": "operate"},
    )
    assert resp.status_code == 403


async def test_list_shares_owner_only(client):
    await seed_user("ls-owner")
    await seed_user("ls-member")
    await _login(client, "ls-owner")
    await _share_with(client, "ls-owner", "ls-audit", "ws-ls-owner", "view")

    await _login(client, "ls-member")
    resp = await client.get("/api/workspaces/ws-ls-owner/shares")
    assert resp.status_code == 403


async def test_admin_delete_persists_preserve_pvc(client, db):
    """The admin delete's preserve_pvc flag is persisted on the workspace row
    so the reconciler can decide whether to tear down the PVC/namespace."""
    await seed_user("adm-delete", is_admin=True)
    await seed_user("del-a")
    await seed_user("del-b")
    await _login(client, "adm-delete")

    keep = await client.delete("/api/admin/workspaces/ws-del-a?preserve_pvc=true")
    assert keep.status_code == 202
    drop = await client.delete("/api/admin/workspaces/ws-del-b")
    assert drop.status_code == 202

    from models import Workspace
    ws_keep = await db.get(Workspace, "ws-del-a")
    await db.refresh(ws_keep)
    assert ws_keep.state == "deleting"
    assert ws_keep.preserve_pvc is True

    ws_drop = await db.get(Workspace, "ws-del-b")
    await db.refresh(ws_drop)
    assert ws_drop.state == "deleting"
    assert ws_drop.preserve_pvc is False
