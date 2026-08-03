"""Security-hardening regression tests.

Covers the audit's highest-severity gateway findings: H1 (unauthenticated
path traversal to the control plane via the health/oidc whitelist), the H2
class (legacy WebSocket catch-all auth), and M7 (client-supplied service
headers forwarded to the control plane).

Encoded-dot (%2e%2e) variants are used at the HTTP layer because httpx
normalizes literal ``..`` at URL construction; the ASGI transport keeps
%2e%2e and decodes it into the scope path — exactly what uvicorn delivers
in production. WebSockets are driven with direct ASGI scopes because httpx
has no WebSocket client.
"""

import main
from conftest import CLUSTER_IP


# ─── _safe_proxy_path unit matrix ──────────────────────────────────────

def test_safe_proxy_path_rejects_dotdot_segment():
    assert not main._safe_proxy_path("/api/health/../api/x", "/api")
    assert not main._safe_proxy_path("/api/../etc", "/api")


def test_safe_proxy_path_rejects_encoded_and_backslash_variants():
    assert not main._safe_proxy_path("/api/health/%2e%2e/api/x", "/api")
    assert not main._safe_proxy_path("/api/health/%2E%2E/api/x", "/api")
    assert not main._safe_proxy_path("/api/%5c..%5capi/x", "/api")
    assert not main._safe_proxy_path("/api/health/..\\api/x", "/api")


def test_safe_proxy_path_accepts_clean_paths():
    assert main._safe_proxy_path("/api/workspaces/ws-alice", "/api")
    assert main._safe_proxy_path("/api", "/api")
    assert main._safe_proxy_path("/api/./health", "/api")


def test_safe_proxy_path_rejects_escape_beyond_root():
    assert not main._safe_proxy_path("/api2/x", "/api")
    assert not main._safe_proxy_path("/workspace/status2/x", "/workspace/status")
    assert main._safe_proxy_path("/workspace/status/health", "/workspace/status")


# ─── H1: unauthenticated path traversal to the control plane ───────────

async def test_api_traversal_rejected_without_session(client, requests_log):
    resp = await client.post("/api/health/%2e%2e/api/workspaces/ws-victim/start")
    assert resp.status_code == 400
    assert resp.json()["error"] == "Invalid path"
    assert requests_log == []  # control plane never contacted


async def test_api_traversal_rejected_even_with_session(client, valid_cookie, requests_log):
    resp = await client.post("/api/health/%2e%2e/api/workspaces/ws-victim/start")
    assert resp.status_code == 400
    assert requests_log == []


async def test_ui_traversal_rejected(client, requests_log):
    resp = await client.get("/ui/%2e%2e/api/workspaces/ws-victim")
    assert resp.status_code == 400
    assert requests_log == []


async def test_workspace_status_traversal_rejected(client, valid_cookie, requests_log):
    resp = await client.get(
        "/workspace/status/%2e%2e/%2e%2e/%2e%2e/%2e%2e/api/internal/workspaces/ws-victim/routing"
    )
    assert resp.status_code == 400
    assert requests_log == []


# ─── M7: client-supplied service headers never reach the control plane ──

async def test_service_headers_not_forwarded_to_control_plane(client, requests_log):
    await client.get(
        "/ui/dashboard",
        headers={
            "X-Service-Auth": "forged",
            "X-Service-User": "victim",
            "X-Forwarded-Host": "evil.example",
        },
    )
    cp = [r for r in requests_log if r.url.host == "control-plane"]
    assert cp, "expected the control-plane proxy path to be exercised"
    last = cp[-1]
    assert "x-service-auth" not in last.headers
    assert "x-service-user" not in last.headers
    assert last.headers.get("x-forwarded-host") != "evil.example"
    assert last.headers.get("x-forwarded-for") == "127.0.0.1"


# ─── H2 class: legacy WebSocket catch-all requires session + token ──────

def _ws_scope(path, cookie=None):
    headers = []
    if cookie:
        headers.append((b"cookie", f"session={cookie}".encode()))
    return {
        "type": "websocket",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "server": ("agents.local.test", 80),
        "client": ("127.0.0.1", 1234),
        "scheme": "ws",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "subprotocols": [],
        "state": {},
    }



async def _drive_ws(app, scope):
    sent = []
    calls = {"n": 0}

    async def receive():
        # First message must be the connect handshake (starlette's
        # accept() blocks on it); anything after is a client disconnect.
        calls["n"] += 1
        if calls["n"] == 1:
            return {"type": "websocket.connect"}
        return {"type": "websocket.disconnect", "code": 1000}

    async def send(msg):
        sent.append(msg)

    await app(scope, receive, send)
    return sent


async def test_ws_canvas_sockets_closed_before_accept_without_session(app, requests_log):
    sent = await _drive_ws(app, _ws_scope("/canvas/sockets"))
    assert not any(m["type"] == "websocket.accept" for m in sent)
    assert any(m["type"] == "websocket.close" and m.get("code") == 1008 for m in sent)
    assert requests_log == []  # closed on sight — no session check, no routing


async def test_ws_unknown_path_closed_1011(app, requests_log):
    sent = await _drive_ws(app, _ws_scope("/nope"))
    assert not any(m["type"] == "websocket.accept" for m in sent)
    assert any(m["type"] == "websocket.close" and m.get("code") == 1011 for m in sent)
    assert requests_log == []

async def test_ws_canvas_sockets_relays_with_session(app, monkeypatch):
    recorded = {}

    async def fake_relay(client_ws, upstream_uri, **kwargs):
        recorded["uri"] = upstream_uri

    monkeypatch.setattr(main, "_relay_ws", fake_relay)
    sent = await _drive_ws(app, _ws_scope("/canvas/sockets", cookie="good-cookie"))
    assert any(m["type"] == "websocket.accept" for m in sent)
    assert recorded.get("uri") == f"ws://{CLUSTER_IP}:8000/sockets"


async def test_ws_chat_relays_with_paseo_token(app, monkeypatch):
    recorded = {}

    async def fake_relay(client_ws, upstream_uri, **kwargs):
        recorded["uri"] = upstream_uri
        recorded["kwargs"] = kwargs

    monkeypatch.setattr(main, "_relay_ws", fake_relay)
    sent = await _drive_ws(app, _ws_scope("/chat/ws", cookie="good-cookie"))
    assert any(m["type"] == "websocket.accept" for m in sent)
    assert recorded.get("uri") == f"ws://{CLUSTER_IP}:6767/ws"
    assert recorded.get("kwargs", {}).get("auth_token") == "paseo-secret"


async def test_ws_relay_refused_when_no_agent_token(app, monkeypatch, routing_config):
    recorded = {}

    async def fake_relay(client_ws, upstream_uri, **kwargs):
        recorded["uri"] = upstream_uri

    monkeypatch.setattr(main, "_relay_ws", fake_relay)
    routing_config["agent_token"] = ""
    sent = await _drive_ws(app, _ws_scope("/canvas/sockets", cookie="good-cookie"))
    assert not any(m["type"] == "websocket.accept" for m in sent)
    assert any(m["type"] == "websocket.close" for m in sent)
    assert recorded == {}  # no unauthenticated relay attempted
