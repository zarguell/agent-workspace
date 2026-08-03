"""Redirect behavior: root and workspace routes with/without a session."""


async def test_root_not_logged_in(client):
    resp = await client.get("/")
    assert resp.status_code == 307
    assert resp.headers["location"].endswith("/ui/login")


async def test_root_logged_in(client, valid_cookie):
    resp = await client.get("/")
    assert resp.status_code == 307
    assert resp.headers["location"].endswith("/ui/workspaces")


async def test_canvas_not_logged_in(client):
    resp = await client.get("/canvas/")
    assert resp.status_code == 307
    assert resp.headers["location"].endswith("/ui/login")


async def test_canvas_invalid_cookie(client):
    client.cookies.set("session", "bad")
    resp = await client.get("/canvas/")
    assert resp.status_code == 307
    assert resp.headers["location"].endswith("/ui/login")


async def test_code_not_logged_in(client):
    resp = await client.get("/code/")
    assert resp.status_code == 307
    assert resp.headers["location"].endswith("/ui/login")


async def test_health_unauthenticated(client):
    resp = await client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["service"] == "gateway"
