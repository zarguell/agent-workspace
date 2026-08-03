"""Auth endpoint tests: login, session, logout, expiry."""

import uuid
from datetime import datetime, timedelta, timezone

from conftest import seed_user


async def _login(client, username, password="pw"):
    return await client.post("/api/login", json={"username": username, "password": password})


async def test_login_success(client):
    await seed_user("alice", is_admin=True)
    resp = await _login(client, "alice")
    assert resp.status_code == 200
    body = resp.json()
    assert body["username"] == "alice"
    assert body["user_id"]
    assert body["is_admin"] is True
    assert "session=" in resp.headers.get("set-cookie", "")


async def test_login_wrong_password(client):
    await seed_user("alice")
    resp = await _login(client, "alice", password="wrong")
    assert resp.status_code == 401
    assert resp.json()["error"] == "Invalid credentials"


async def test_login_unknown_user(client):
    resp = await _login(client, "ghost")
    assert resp.status_code == 401


async def test_login_auto_creates_workspace(client):
    await seed_user("autocreate", create_workspace=False)
    resp = await _login(client, "autocreate")
    assert resp.status_code == 200

    ws_resp = await client.get("/api/workspaces")
    assert ws_resp.status_code == 200
    workspaces = ws_resp.json()
    assert len(workspaces) == 1
    assert workspaces[0]["workspace_id"] == "ws-autocreate"
    assert workspaces[0]["state"] == "requested"


async def test_session_valid(client):
    await seed_user("alice")
    await _login(client, "alice")
    resp = await client.get("/api/session")
    assert resp.status_code == 200
    body = resp.json()
    assert body["username"] == "alice"
    assert body["session_id"]


async def test_session_no_cookie(client):
    resp = await client.get("/api/session")
    assert resp.status_code == 401


async def test_logout_invalidates_session(client):
    await seed_user("alice")
    await _login(client, "alice")
    old_cookie = client.cookies.get("session")
    assert old_cookie

    resp = await client.post("/api/logout")
    assert resp.status_code == 200

    # Re-apply the stale cookie — the session row is gone from the DB.
    client.cookies.set("session", old_cookie)
    session_resp = await client.get("/api/session")
    assert session_resp.status_code == 401


async def test_expired_session(client, db):
    user = await seed_user("expired")
    now = datetime.now(timezone.utc)
    expired_id = str(uuid.uuid4())

    from models import Session
    db.add(Session(
        session_id=expired_id,
        user_id=user.user_id,
        created_at=now - timedelta(hours=2),
        expires_at=now - timedelta(hours=1),
    ))
    await db.commit()

    client.cookies.set("session", expired_id)
    resp = await client.get("/api/session")
    assert resp.status_code == 401
