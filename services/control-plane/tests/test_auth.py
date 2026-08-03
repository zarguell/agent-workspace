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


# ─── Login hardening: SSO accounts + timing equalization ───────────────

async def _seed_sso_user(db, username, sub):
    """Idempotent SSO user seeding — username and oidc_sub are unique and the
    test DB persists across runs."""
    from sqlalchemy import select
    from models import User
    existing = (await db.execute(select(User).where(User.username == username))).scalar_one_or_none()
    if existing is None:
        db.add(User(
            username=username,
            password_hash="!",  # oidc.py provisioning sets this for SSO users
            display_name=username.capitalize(),
            is_admin=True,
            oidc_sub=sub,
        ))
        await db.commit()



async def test_sso_user_login_is_401_not_500(client, db):
    """SSO accounts store password_hash="!" — bcrypt raises ValueError on it,
    which must surface as a uniform 401, never a 500."""
    await _seed_sso_user(db, "sso-login", "sub-sso-login")

    resp = await _login(client, "sso-login")
    assert resp.status_code == 401
    assert resp.json()["error"] == "Invalid credentials"


async def test_authenticate_sso_user_returns_none_not_raises(db):
    """Unit-level: the ValueError from bcrypt on "!" is caught and treated
    as invalid credentials."""
    from auth import authenticate_user
    await _seed_sso_user(db, "sso-unit", "sub-sso-unit")

    result = await authenticate_user(db, "sso-unit", "any-password")
    assert result is None


async def test_unknown_user_still_runs_bcrypt_check(monkeypatch):
    """The unknown-username path must run a bcrypt check against a fixed
    dummy hash so response time does not reveal user existence."""
    import auth as auth_mod
    calls = []

    def fake_verify(password, password_hash):
        calls.append((password, password_hash))
        return False

    monkeypatch.setattr(auth_mod, "verify_password", fake_verify)

    from database import async_session_factory
    async with async_session_factory() as db:
        user = await auth_mod.authenticate_user(db, "no-such-user", "whatever")

    assert user is None
    assert calls == [("whatever", auth_mod._DUMMY_PASSWORD_HASH)]

async def test_login_throttled_after_max_attempts(client):
    await seed_user("throttle-user")

    for _ in range(10):
        resp = await _login(client, "throttle-user", password="wrong")
        assert resp.status_code == 401

    # The 11th attempt within the window is rejected, regardless of password.
    blocked = await _login(client, "throttle-user", password="wrong")
    assert blocked.status_code == 429
    assert blocked.json()["error"] == "Too many login attempts; try again later"

    correct = await _login(client, "throttle-user", password="pw")
    assert correct.status_code == 429


async def test_login_throttle_resets_after_window(client):
    await seed_user("throttle-reset")

    for _ in range(10):
        await _login(client, "throttle-reset", password="wrong")
    assert (await _login(client, "throttle-reset", password="wrong")).status_code == 429

    # A fresh window (simulated by clearing the attempt store) allows login.
    from main import LOGIN_ATTEMPT_STORE
    LOGIN_ATTEMPT_STORE.clear()
    resp = await _login(client, "throttle-reset", password="pw")
    assert resp.status_code == 200
