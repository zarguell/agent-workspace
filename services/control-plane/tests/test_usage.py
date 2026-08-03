"""Usage ledger ingestion, quota enforcement, and summary endpoints.

All tests use dedicated usernames: alice/bob carry admin flags and grants
set by other test files, which would corrupt admin-gated assertions.
"""

from conftest import seed_user


async def _login(client, username, password="pw"):
    return await client.post("/api/login", json={"username": username, "password": password})


async def _ingest(client, token, username, events):
    return await client.post(
        "/api/internal/usage",
        headers={"X-Service-Auth": token, "X-Service-User": username},
        json={"events": events},
    )


async def test_ingest_requires_service_auth(client):
    resp = await client.post("/api/internal/usage", json={"events": []})
    assert resp.status_code == 403


async def test_ingest_requires_service_user(client, service_token):
    resp = await client.post(
        "/api/internal/usage",
        headers={"X-Service-Auth": service_token},
        json={"events": [{"category": "tokens", "metric": "t", "amount": 1, "unit": "tokens"}]},
    )
    assert resp.status_code == 401


async def test_ingest_unknown_user(client, service_token):
    resp = await _ingest(client, service_token, "ghost", [
        {"category": "tokens", "metric": "t", "amount": 1, "unit": "tokens"},
    ])
    assert resp.status_code == 404


async def test_ingest_unknown_category(client, service_token):
    await seed_user("u-cat")
    resp = await _ingest(client, service_token, "u-cat", [
        {"category": "bananas", "metric": "t", "amount": 1, "unit": "tokens"},
    ])
    assert resp.status_code == 400


async def test_ingest_records_and_summarizes(client, service_token):
    await seed_user("u-rec")
    await _login(client, "u-rec")

    resp = await _ingest(client, service_token, "u-rec", [
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


async def test_quota_exceeded_returns_429(client, service_token):
    await seed_user("uq-user")
    await seed_user("uq-admin", is_admin=True)
    user = await seed_user("uq-user")
    await _login(client, "uq-admin")

    set_q = await client.put(
        f"/api/admin/quotas/{user.user_id}",
        json={"max_monthly_tokens": 100},
    )
    assert set_q.status_code == 200

    first = await _ingest(client, service_token, "uq-user", [
        {"category": "tokens", "metric": "t", "amount": 60, "unit": "tokens"},
    ])
    assert first.status_code == 201
    assert first.json()["quota_exceeded"] is False

    second = await _ingest(client, service_token, "uq-user", [
        {"category": "tokens", "metric": "t", "amount": 60, "unit": "tokens"},
    ])
    assert second.status_code == 429
    assert second.json()["quota_exceeded"] is True

    # Events are still recorded for full accounting; the summary flags it.
    await _login(client, "uq-user")
    summary = await client.get("/api/usage/summary")
    body = summary.json()
    assert body["totals"]["tokens"] == 120
    assert body["max_monthly_tokens"] == 100
    assert body["tokens_remaining"] == -20
    assert body["quota_exceeded"] is True


async def test_summary_requires_session(client):
    resp = await client.get("/api/usage/summary")
    assert resp.status_code == 401


async def test_admin_usage_filtered(client, service_token):
    await seed_user("uf-user")
    await seed_user("uf-admin", is_admin=True)
    await _login(client, "uf-user")
    await _ingest(client, service_token, "uf-user", [
        {"category": "tokens", "metric": "t", "amount": 500, "unit": "tokens", "workspace_id": "ws-uf-user"},
    ])

    # Non-admin cannot list usage.
    denied = await client.get("/api/admin/usage")
    assert denied.status_code == 403

    await _login(client, "uf-admin")
    page = await client.get("/api/admin/usage?workspace_id=ws-uf-user")
    assert page.json()["total"] == 1
    assert page.json()["events"][0]["amount"] == 500

    other = await client.get("/api/admin/usage?workspace_id=ws-nope")
    assert other.json()["total"] == 0


async def test_admin_quota_crud(client):
    user = await seed_user("qc-user")
    await seed_user("qc-admin", is_admin=True)
    await _login(client, "qc-user")

    # Non-admin cannot touch quotas.
    denied = await client.get(f"/api/admin/quotas/{user.user_id}")
    assert denied.status_code == 403

    await _login(client, "qc-admin")
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
