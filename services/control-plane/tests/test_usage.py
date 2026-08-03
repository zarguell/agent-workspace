"""Usage ledger ingestion, quota enforcement, and summary endpoints.

Ingestion is pod-originated: the caller authenticates with the workspace's
per-workspace token (X-Workspace-Token) and identity is derived from the
token's workspace owner. X-Service-User is not accepted on this endpoint.

All tests use dedicated usernames: alice/bob carry admin flags and grants
set by other test files, which would corrupt admin-gated assertions.
"""

from conftest import seed_user


async def _login(client, username, password="pw"):
    return await client.post("/api/login", json={"username": username, "password": password})


async def _ingest(client, username, events, extra_headers=None):
    headers = {"X-Workspace-Token": f"tok-{username}"}
    if extra_headers:
        headers.update(extra_headers)
    return await client.post(
        "/api/internal/usage",
        headers=headers,
        json={"events": events},
    )


async def test_ingest_does_not_require_service_auth(client):
    """Usage is pod-originated: the shared X-Service-Auth secret is not used."""
    await seed_user("ua-nosvc")
    resp = await client.post(
        "/api/internal/usage",
        headers={"X-Workspace-Token": "tok-ua-nosvc"},
        json={"events": [{"category": "tokens", "metric": "t", "amount": 1, "unit": "tokens"}]},
    )
    assert resp.status_code == 201


async def test_ingest_requires_workspace_token(client):
    resp = await client.post(
        "/api/internal/usage",
        json={"events": [{"category": "tokens", "metric": "t", "amount": 1, "unit": "tokens"}]},
    )
    assert resp.status_code == 401


async def test_ingest_unknown_token(client):
    resp = await _ingest(client, "ua-ghost", [
        {"category": "tokens", "metric": "t", "amount": 1, "unit": "tokens"},
    ])
    assert resp.status_code == 403


async def test_ingest_unknown_category(client):
    await seed_user("ua-cat")
    resp = await _ingest(client, "ua-cat", [
        {"category": "bananas", "metric": "t", "amount": 1, "unit": "tokens"},
    ])
    assert resp.status_code == 400


async def test_ingest_records_and_summarizes(client):
    await seed_user("ua-rec")
    await _login(client, "ua-rec")

    resp = await _ingest(client, "ua-rec", [
        {"category": "tokens", "metric": "claude_code_tokens", "amount": 1200, "unit": "tokens"},
        {"category": "compute", "metric": "cpu_seconds", "amount": 3600, "unit": "s"},
    ])
    assert resp.status_code == 201
    assert resp.json()["ok"] is True

    summary = await client.get("/api/usage/summary")
    assert summary.status_code == 200
    body = summary.json()
    assert body["totals"]["tokens"] == 1200
    assert body["totals"]["compute"] == 3600
    assert body["max_monthly_tokens"] is None  # unlimited by default
    assert body["quota_exceeded"] is False


async def test_ingest_spoofed_service_user_ignored(client):
    """Identity comes from the token's workspace owner; X-Service-User is ignored."""
    owner = await seed_user("ua-owner")
    spoof = await seed_user("ua-spoof")
    await _login(client, "ua-owner")

    resp = await _ingest(
        client, "ua-owner",
        [{"category": "tokens", "metric": "t", "amount": 500, "unit": "tokens"}],
        extra_headers={"X-Service-User": "ua-spoof"},
    )
    assert resp.status_code == 201

    # Events land under the token owner, not the spoofed user.
    summary = await client.get("/api/usage/summary")
    assert summary.json()["totals"]["tokens"] == 500

    await seed_user("ua-admin", is_admin=True)
    await _login(client, "ua-admin")
    spoofed = await client.get(f"/api/admin/usage?user_id={spoof.user_id}")
    assert spoofed.json()["total"] == 0
    owned = await client.get(f"/api/admin/usage?user_id={owner.user_id}")
    assert owned.json()["total"] == 1
    assert owned.json()["events"][0]["workspace_id"] == "ws-ua-owner"


async def test_ingest_forces_bound_workspace(client):
    """Client-supplied workspace_id in the event body is ignored."""
    owner = await seed_user("ua-bind")
    await seed_user("ua-bind-admin", is_admin=True)
    await _login(client, "ua-bind-admin")

    resp = await _ingest(client, "ua-bind", [
        {"category": "tokens", "metric": "t", "amount": 250, "unit": "tokens", "workspace_id": "ws-someone-elses"},
    ])
    assert resp.status_code == 201

    page = await client.get(f"/api/admin/usage?user_id={owner.user_id}")
    assert page.json()["total"] == 1
    assert page.json()["events"][0]["workspace_id"] == "ws-ua-bind"


async def test_quota_exceeded_returns_429(client):
    user = await seed_user("ua-quota")
    await seed_user("ua-quota-admin", is_admin=True)
    await _login(client, "ua-quota-admin")

    set_q = await client.put(
        f"/api/admin/quotas/{user.user_id}",
        json={"max_monthly_tokens": 100},
    )
    assert set_q.status_code == 200

    first = await _ingest(client, "ua-quota", [
        {"category": "tokens", "metric": "t", "amount": 60, "unit": "tokens"},
    ])
    assert first.status_code == 201
    assert first.json()["quota_exceeded"] is False

    second = await _ingest(client, "ua-quota", [
        {"category": "tokens", "metric": "t", "amount": 60, "unit": "tokens"},
    ])
    assert second.status_code == 429
    assert second.json()["quota_exceeded"] is True

    # Events are still recorded for full accounting; the summary flags it.
    await _login(client, "ua-quota")
    summary = await client.get("/api/usage/summary")
    body = summary.json()
    assert body["totals"]["tokens"] == 120
    assert body["max_monthly_tokens"] == 100
    assert body["tokens_remaining"] == -20
    assert body["quota_exceeded"] is True


async def test_summary_requires_session(client):
    resp = await client.get("/api/usage/summary")
    assert resp.status_code == 401


async def test_admin_usage_filtered(client):
    await seed_user("ua-uf")
    await seed_user("ua-uf-admin", is_admin=True)
    await _login(client, "ua-uf")
    await _ingest(client, "ua-uf", [
        {"category": "tokens", "metric": "t", "amount": 500, "unit": "tokens"},
    ])

    # Non-admin cannot list usage.
    denied = await client.get("/api/admin/usage")
    assert denied.status_code == 403

    await _login(client, "ua-uf-admin")
    page = await client.get("/api/admin/usage?workspace_id=ws-ua-uf")
    assert page.json()["total"] == 1
    assert page.json()["events"][0]["amount"] == 500

    other = await client.get("/api/admin/usage?workspace_id=ws-nope")
    assert other.json()["total"] == 0


async def test_admin_quota_crud(client):
    user = await seed_user("ua-qc")
    await seed_user("ua-qc-admin", is_admin=True)
    await _login(client, "ua-qc")

    # Non-admin cannot touch quotas.
    denied = await client.get(f"/api/admin/quotas/{user.user_id}")
    assert denied.status_code == 403

    await _login(client, "ua-qc-admin")
    created = await client.put(
        f"/api/admin/quotas/{user.user_id}",
        json={"max_monthly_tokens": 10000, "max_storage_gb": 20},
    )
    assert created.status_code == 200
    assert created.json()["max_monthly_tokens"] == 10000
    assert created.json()["max_storage_gb"] == 20

    fetched = await client.get(f"/api/admin/quotas/{user.user_id}")
    assert fetched.status_code == 200
    assert fetched.json()["max_monthly_tokens"] == 10000

    # Partial update keeps the other field.
    updated = await client.put(
        f"/api/admin/quotas/{user.user_id}",
        json={"max_monthly_tokens": 5000},
    )
    assert updated.json()["max_monthly_tokens"] == 5000
    assert updated.json()["max_storage_gb"] == 20
