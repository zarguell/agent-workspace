"""Response rewriting: gzipped HTML re-encode (no content-encoding leftover)
and Location header rewrites under the gateway prefix on every proxy path."""

from conftest import CLUSTER_IP, _gzip_config, _redirect_config


# ─── Gzipped upstream HTML ─────────────────────────────────────────────

async def test_canvas_gzipped_html_strips_encoding_headers(client, valid_cookie):
    _gzip_config["upstream_html"] = True
    resp = await client.get("/canvas/")
    assert resp.status_code == 200
    assert "content-encoding" not in resp.headers
    # The decompressed body was rewritten and re-encoded — no blank page.
    assert 'src="/canvas/assets/app.js"' in resp.text
    # No length mismatch: declared length matches the actual body.
    assert int(resp.headers["content-length"]) == len(resp.content)


async def test_paseo_gzipped_html_strips_encoding_headers(client, valid_cookie):
    _gzip_config["upstream_html"] = True
    resp = await client.get("/workspace/chat/")
    assert resp.status_code == 200
    assert "content-encoding" not in resp.headers
    assert "Canvas" in resp.text  # decompressed HTML survives
    assert int(resp.headers["content-length"]) == len(resp.content)


# ─── Location rewriting: legacy middleware (canvas/code/chat) ───────────

async def test_legacy_proxy_rewrites_relative_location(client, valid_cookie):
    _redirect_config["location"] = "/login"
    resp = await client.get("/canvas/")
    assert resp.status_code == 302
    assert resp.headers["location"] == "/canvas/login"


async def test_legacy_proxy_rewrites_absolute_upstream_location(client, valid_cookie):
    _redirect_config["location"] = f"http://{CLUSTER_IP}:8000/login"
    resp = await client.get("/canvas/")
    assert resp.status_code == 302
    assert resp.headers["location"] == "/canvas/login"


async def test_legacy_code_proxy_rewrites_location(client, valid_cookie):
    _redirect_config["location"] = "/login"
    resp = await client.get("/code/")
    assert resp.status_code == 302
    assert resp.headers["location"] == "/code/login"


async def test_legacy_proxy_keeps_prefixed_location(client, valid_cookie):
    _redirect_config["location"] = "/canvas/foo"
    resp = await client.get("/canvas/")
    assert resp.headers["location"] == "/canvas/foo"


async def test_external_location_passthrough(client, valid_cookie):
    _redirect_config["location"] = "https://example.com/oauth"
    resp = await client.get("/canvas/")
    assert resp.headers["location"] == "https://example.com/oauth"


# ─── Location rewriting: proxy_http (/workspace/code) ───────────────────

async def test_workspace_code_rewrites_location(client, valid_cookie):
    _redirect_config["location"] = "/login"
    resp = await client.get("/workspace/code/")
    assert resp.status_code == 302
    assert resp.headers["location"] == "/workspace/code/login"


async def test_workspace_code_rewrites_absolute_location(client, valid_cookie):
    _redirect_config["location"] = f"http://{CLUSTER_IP}:8080/login"
    resp = await client.get("/workspace/code/")
    assert resp.status_code == 302
    assert resp.headers["location"] == "/workspace/code/login"


# ─── Location rewriting: _proxy_paseo (/workspace/chat) ─────────────────

async def test_paseo_rewrites_location(client, valid_cookie):
    _redirect_config["location"] = "/login"
    resp = await client.get("/workspace/chat/")
    assert resp.status_code == 302
    assert resp.headers["location"] == "/workspace/chat/login"


async def test_paseo_rewrites_absolute_location(client, valid_cookie):
    _redirect_config["location"] = f"http://{CLUSTER_IP}:6767/login"
    resp = await client.get("/workspace/chat/")
    assert resp.status_code == 302
    assert resp.headers["location"] == "/workspace/chat/login"
