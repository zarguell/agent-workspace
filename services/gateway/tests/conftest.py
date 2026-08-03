"""Test fixtures for the gateway suite — the control plane is fully mocked.

Two things need the mock: the module-global ``main.http_client`` (used by
validate_session / get_routing_status / get_mcp_target / trigger_start /
record_audit) and every fresh ``httpx.AsyncClient()`` the gateway constructs
inline (path_proxy, login_proxy, api_proxy, _proxy_paseo). The app fixture
patches ``main.httpx.AsyncClient`` with a factory that returns a
MockTransport-backed client for no-arg construction and delegates to the
real class when a transport is supplied (so the test client itself is
unaffected).

``os.chdir`` to the service dir is required: main.py loads templates from
the relative path "templates".
"""

import os
import gzip
import sys
from pathlib import Path

SRV = Path(__file__).resolve().parent.parent
os.chdir(SRV)
sys.path.insert(0, str(SRV))

os.environ.setdefault("SERVICE_AUTH_TOKEN", "test-token")
os.environ.setdefault("CONTROL_PLANE_URL", "http://control-plane:80")

import httpx
import pytest
import pytest_asyncio

CP = "http://control-plane:80"
CLUSTER_IP = "10.0.0.5"
GOOD_COOKIE = "good-cookie"

UPSTREAM_HTML = (
    '<!DOCTYPE html><html><head><title>Canvas</title></head>'
    '<body><script src="/assets/app.js"></script></body></html>'
)

MCP_RESPONSE = {
    "jsonrpc": "2.0",
    "id": 1,
    "result": {"tools": [{"name": "demo", "description": "demo tool"}]},
}

# Per-test upstream knobs (all reset to these defaults by the autouse fixture).
_gzip_config = {"upstream_html": False}
_redirect_config = {"location": None}
_login_config = {"set_cookie": "session=abc; Path=/; HttpOnly"}
_user_config = {"set_cookie": "workspace=ws-alice; Path=/; HttpOnly"}

# Mutable per-test state read by the mock handler.
_requests_log: list = []
_routing_config = {"state": "running", "agent_ready": True, "cluster_ip": CLUSTER_IP, "agent_token": "tok-alice"}

# Session cookie marker → session dict.
_SESSIONS = {
    f"session={GOOD_COOKIE}": {"user_id": "u-alice", "username": "alice", "display_name": "Alice", "is_admin": True},
    "session=bob-cookie": {"user_id": "u-bob", "username": "bob", "display_name": "Bob", "is_admin": False},
    "session=eve-cookie": {"user_id": "u-eve", "username": "eve", "display_name": "Eve", "is_admin": False},
}

# Workspace → users allowed to route to it (mirrors the control plane:
# owner alice + bob who holds an operate share on ws-alice).
_ROUTING_ACL = {"ws-alice": {"alice", "bob"}}

# MCP servers available per workspace (only mcp-1 exists and is enabled).
_MCP_SERVERS = {"ws-alice": {"mcp-1": 3001}}


def _handler(request: httpx.Request) -> httpx.Response:
    _requests_log.append(request)
    url = request.url

    if url.host == "control-plane":
        path = url.path
        if request.method == "GET" and path == "/api/session":
            cookie = request.headers.get("cookie", "")
            for marker, session in _SESSIONS.items():
                if marker in cookie:
                    return httpx.Response(200, json=session)
            return httpx.Response(401, json={"error": "No valid session"})

        if request.method == "GET" and path == "/api/oidc/config":
            return httpx.Response(200, json={"enabled": False, "issuer": None})
        if request.method == "GET" and path == "/api/oidc/callback":
            return httpx.Response(400, json={"error": "Invalid state"})

        if request.method == "GET" and path.startswith("/api/internal/workspaces/") and path.endswith("/routing"):
            ws_id = path.split("/")[4]
            service_user = request.headers.get("x-service-user", "")
            if service_user not in _ROUTING_ACL.get(ws_id, set()):
                return httpx.Response(404, json={"error": "Not found"})
            return httpx.Response(200, json={
                "workspace_id": ws_id,
                "state": _routing_config["state"],
                "cluster_ip": _routing_config["cluster_ip"],
                "agent_ready": _routing_config["agent_ready"],
                "agent_token": _routing_config["agent_token"],
                "exposures": [{"id": "exp-1", "port": 3000}],
            })

        if request.method == "GET" and path.startswith("/api/internal/workspaces/") and "/mcp/" in path:
            # /api/internal/workspaces/{ws}/mcp/{server_id}
            parts = path.split("/")
            ws_id = parts[4]
            server_id = parts[6]
            service_user = request.headers.get("x-service-user", "")
            port = _MCP_SERVERS.get(ws_id, {}).get(server_id)
            if service_user in _ROUTING_ACL.get(ws_id, set()) and port is not None:
                return httpx.Response(200, json={
                    "workspace_id": ws_id,
                    "server_id": server_id,
                    "name": "tools",
                    "port": port,
                    "enabled": True,
                })
            return httpx.Response(404, json={"error": "Not found"})

        if request.method == "POST" and path == "/api/internal/activity":
            return httpx.Response(204)
        if request.method == "POST" and path == "/api/login":
            return httpx.Response(200, json={"ok": True}, headers={"set-cookie": _login_config["set_cookie"]})
        if request.method == "GET" and path == "/api/user":
            return httpx.Response(200, json={"user": "alice"}, headers={"set-cookie": _user_config["set_cookie"]})
        if request.method == "POST" and path.startswith("/api/workspaces/") and path.endswith("/start"):
            return httpx.Response(202, json={
                "workspace_id": path.split("/")[3],
                "state": "starting",
            })
        if request.method == "POST" and path == "/api/audit":
            return httpx.Response(201, json={"ok": True})
        return httpx.Response(404, json={"error": "not found"})

    if url.host == CLUSTER_IP:
        if url.port == 3001:
            return httpx.Response(200, json=MCP_RESPONSE, headers={"content-type": "application/json"})
        if _redirect_config["location"]:
            return httpx.Response(302, headers={"location": _redirect_config["location"]})
        if url.path == "/password":
            return httpx.Response(200, json={"password": "paseo-secret"})
        if _gzip_config["upstream_html"]:
            return httpx.Response(
                200,
                content=gzip.compress(UPSTREAM_HTML.encode()),
                headers={"content-type": "text/html", "content-encoding": "gzip"},
            )
        return httpx.Response(200, text=UPSTREAM_HTML, headers={"content-type": "text/html"})

    return httpx.Response(404, json={"error": "not found"})


@pytest.fixture(autouse=True)
def _reset_mocks():
    _requests_log.clear()
    _routing_config.update(state="running", agent_ready=True, cluster_ip=CLUSTER_IP, agent_token="tok-alice")
    _gzip_config["upstream_html"] = False
    _redirect_config["location"] = None
    _login_config["set_cookie"] = "session=abc; Path=/; HttpOnly"
    _user_config["set_cookie"] = "workspace=ws-alice; Path=/; HttpOnly"
    try:
        import main as _gw
        _gw._LAST_ACTIVITY_TOUCH.clear()
    except ImportError:
        pass  # main not imported yet — nothing to reset
    yield
    _requests_log.clear()


@pytest.fixture
def routing_config():
    return _routing_config


@pytest.fixture
def requests_log():
    return _requests_log


@pytest_asyncio.fixture
async def valid_cookie(client):
    """Set the known-good session cookie on the client."""
    client.cookies.set("session", GOOD_COOKIE)
    return client.cookies


@pytest_asyncio.fixture
async def app(monkeypatch):
    import main

    real_ac = httpx.AsyncClient

    def _factory(*args, **kwargs):
        # The gateway constructs httpx.AsyncClient() with no args; the test
        # client passes transport= explicitly and must stay on the real class.
        if args or kwargs.get("transport"):
            return real_ac(*args, **kwargs)
        return real_ac(transport=httpx.MockTransport(_handler))

    monkeypatch.setattr(main.httpx, "AsyncClient", _factory)
    main.http_client = real_ac(transport=httpx.MockTransport(_handler))
    try:
        yield main.app
    finally:
        await main.http_client.aclose()


@pytest_asyncio.fixture
async def client(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://agents.local.test") as c:
        yield c
