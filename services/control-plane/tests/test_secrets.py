"""Workspace secrets: CRUD, permissions, encryption at rest."""


import pytest

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


async def _set_secret(client, workspace_id, key, value):
    return await client.put(
        f"/api/workspaces/{workspace_id}/secrets/{key}",
        json={"value": value},
    )


async def test_upsert_get_update_delete(client):
    await seed_user("s-owner")
    await _login(client, "s-owner")

    created = await _set_secret(client, "ws-s-owner", "API_KEY", "secret-value-1")
    assert created.status_code == 200
    assert created.json()["key"] == "API_KEY"
    fetched = await client.get("/api/workspaces/ws-s-owner/secrets/API_KEY")
    assert fetched.status_code == 200
    assert fetched.json()["value"] == "secret-value-1"

    updated = await _set_secret(client, "ws-s-owner", "API_KEY", "secret-value-2")
    assert updated.status_code == 200
    assert (await client.get("/api/workspaces/ws-s-owner/secrets/API_KEY")).json()["value"] == "secret-value-2"

    listed = await client.get("/api/workspaces/ws-s-owner/secrets")
    assert [s["key"] for s in listed.json()] == ["API_KEY"]

    deleted = await client.delete("/api/workspaces/ws-s-owner/secrets/API_KEY")
    assert deleted.status_code == 200
    assert (await client.get("/api/workspaces/ws-s-owner/secrets")).json() == []


async def test_value_encrypted_at_rest(client, db):
    await seed_user("se-owner")
    await _login(client, "se-owner")
    await _set_secret(client, "ws-se-owner", "DB_PASSWORD", "hunter2")

    from models import WorkspaceSecret
    row = await db.get(WorkspaceSecret, {"workspace_id": "ws-se-owner", "key": "DB_PASSWORD"})
    assert row is not None
    assert row.value_encrypted != "hunter2"
    assert "hunter2" not in row.value_encrypted

    from secrets_store import decrypt_value
    assert decrypt_value(row.value_encrypted) == "hunter2"


async def test_get_requires_operate(client):
    await seed_user("sv-owner")
    await seed_user("sv-viewer")
    await _login(client, "sv-owner")
    group_id = await _share_with(client, "sv-secret-viewers", "ws-sv-owner", "view")
    await client.post(f"/api/groups/{group_id}/members", json={"username": "sv-viewer"})
    await _set_secret(client, "ws-sv-owner", "K", "v")

    await _login(client, "sv-viewer")
    # View can list names…
    listed = await client.get("/api/workspaces/ws-sv-owner/secrets")
    assert listed.status_code == 200
    assert [s["key"] for s in listed.json()] == ["K"]
    # …but cannot read or write values.
    assert (await client.get("/api/workspaces/ws-sv-owner/secrets/K")).status_code == 403
    assert (await _set_secret(client, "ws-sv-owner", "K2", "v")).status_code == 403


async def test_member_with_operate_can_manage(client):
    await seed_user("sm-owner")
    await seed_user("sm-member")
    await _login(client, "sm-owner")
    group_id = await _share_with(client, "sm-ops", "ws-sm-owner", "operate")
    await client.post(f"/api/groups/{group_id}/members", json={"username": "sm-member"})

    await _login(client, "sm-member")
    assert (await _set_secret(client, "ws-sm-owner", "TOKEN", "abc")).status_code == 200
    assert (await client.get("/api/workspaces/ws-sm-owner/secrets/TOKEN")).json()["value"] == "abc"


async def test_stranger_denied(client):
    await seed_user("st-owner")
    await seed_user("st-stranger")
    await _login(client, "st-owner")
    await _share_with(client, "st-closed", "ws-st-owner", "operate")  # stranger not a member

    await _login(client, "st-stranger")
    assert (await client.get("/api/workspaces/ws-st-owner/secrets")).status_code == 404
    assert (await _set_secret(client, "ws-st-owner", "K", "v")).status_code == 404


async def test_invalid_key_rejected(client):
    await seed_user("sk-owner")
    await _login(client, "sk-owner")
    resp = await _set_secret(client, "ws-sk-owner", "bad key!", "v")
    assert resp.status_code == 400


async def test_decrypt_value_raises_on_invalid_token(monkeypatch):
    """A secret encrypted under a previous SECRETS_MASTER_KEY must raise a
    clear error, never silently return ''."""
    import secrets_store
    from cryptography.fernet import Fernet

    old_key = Fernet.generate_key().decode()
    new_key = Fernet.generate_key().decode()

    monkeypatch.setenv("SECRETS_MASTER_KEY", old_key)
    secrets_store._fernet = None
    blob = secrets_store.encrypt_value("hunter2")

    monkeypatch.setenv("SECRETS_MASTER_KEY", new_key)  # simulated key rotation
    secrets_store._fernet = None
    try:
        with pytest.raises(secrets_store.SecretDecryptionError):
            secrets_store.decrypt_value(blob)
    finally:
        # Re-init lazily from the (restored) conftest key for later tests.
        secrets_store._fernet = None


async def test_get_secret_returns_500_on_decrypt_failure(app, db, monkeypatch):
    """A lost/rotated SECRETS_MASTER_KEY must surface as a loud 500 instead
    of a silently-empty secret value."""
    import httpx
    import secrets_store
    from cryptography.fernet import Fernet
    from models import WorkspaceSecret

    # Starlette 1.x re-raises exceptions after sending its 500 response (so
    # servers can log them), which test clients see as a raised error unless
    # they opt out of re-raising.
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        owner = await seed_user("rot-owner")
        await client.post("/api/login", json={"username": "rot-owner", "password": "pw"})

        key1 = Fernet.generate_key().decode()
        key2 = Fernet.generate_key().decode()
        monkeypatch.setenv("SECRETS_MASTER_KEY", key1)
        secrets_store._fernet = None
        from secrets_store import encrypt_value
        encrypted = encrypt_value("top-secret")

        # The test DB persists across runs; drop any row from a prior run.
        existing = await db.get(WorkspaceSecret, {"workspace_id": "ws-rot-owner", "key": "API_KEY"})
        if existing is not None:
            await db.delete(existing)
        db.add(WorkspaceSecret(
            workspace_id="ws-rot-owner",
            key="API_KEY",
            value_encrypted=encrypted,
            created_by=owner.user_id,
        ))
        await db.commit()

        monkeypatch.setenv("SECRETS_MASTER_KEY", key2)
        secrets_store._fernet = None
        try:
            resp = await client.get("/api/workspaces/ws-rot-owner/secrets/API_KEY")
            assert resp.status_code == 500
        finally:
            secrets_store._fernet = None
