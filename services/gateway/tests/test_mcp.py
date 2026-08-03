"""Gateway MCP proxy: session + RBAC authz, JSON-RPC passthrough."""

from conftest import CLUSTER_IP, MCP_RESPONSE

MCP_BODY = {"jsonrpc": "2.0", "id": 7, "method": "tools/list", "params": {}}


def _mcp_upstream(requests_log):
    return [r for r in requests_log if r.url.host == CLUSTER_IP and r.url.port == 3001]


async def test_mcp_not_logged_in(client):
    resp = await client.post("/mcp/mcp-1", json=MCP_BODY)
    assert resp.status_code == 307
    assert resp.headers["location"].endswith("/ui/login")


async def test_mcp_stranger_blocked(client, requests_log):
    client.cookies.set("session", "eve-cookie")
    resp = await client.post("/mcp/mcp-1", json=MCP_BODY)
    assert resp.status_code == 503  # routing denied → no proxy
    assert "Could not resolve workspace routing information." in resp.text
    assert _mcp_upstream(requests_log) == []


async def test_mcp_round_trip(client, valid_cookie, requests_log):
    resp = await client.post("/mcp/mcp-1", json=MCP_BODY)
    assert resp.status_code == 200
    assert resp.json() == MCP_RESPONSE

    # The request was proxied to the workspace's MCP port, prefix stripped.
    upstream = _mcp_upstream(requests_log)
    assert len(upstream) == 1
    assert str(upstream[0].url).rstrip("/") == f"http://{CLUSTER_IP}:3001"
    import json as _json
    assert _json.loads(upstream[0].read()) == MCP_BODY


async def test_mcp_subpath_preserved(client, valid_cookie, requests_log):
    resp = await client.post("/mcp/mcp-1/messages", json=MCP_BODY)
    assert resp.status_code == 200
    upstream = _mcp_upstream(requests_log)
    assert str(upstream[0].url) == f"http://{CLUSTER_IP}:3001/messages"


async def test_mcp_unknown_server(client, valid_cookie, requests_log):
    resp = await client.post("/mcp/mcp-zzz", json=MCP_BODY)
    assert resp.status_code == 404
    assert "MCP server not found." in resp.text
    assert _mcp_upstream(requests_log) == []


async def test_mcp_missing_server_id(client, valid_cookie):
    resp = await client.post("/mcp", json=MCP_BODY)
    assert resp.status_code == 404
    assert "Missing MCP server id." in resp.text


async def test_mcp_shared_workspace_via_cookie(client, requests_log):
    client.cookies.set("session", "bob-cookie")
    client.cookies.set("workspace", "ws-alice")
    resp = await client.post("/mcp/mcp-1", json=MCP_BODY)
    assert resp.status_code == 200
    upstream = _mcp_upstream(requests_log)
    assert str(upstream[0].url).rstrip("/") == f"http://{CLUSTER_IP}:3001"
