import logging
logger = logging.getLogger("gateway")
import re
"""
Gateway — session-aware reverse proxy for the agent workspace platform.
"""

import os
import asyncio
import time
from contextlib import asynccontextmanager

import httpx
import websockets
from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect
from starlette.background import BackgroundTask
from fastapi.responses import HTMLResponse, StreamingResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader, TemplateNotFound

# ─── Configuration ───────────────────────────────────────────────────────

CONTROL_PLANE_URL = os.environ.get("CONTROL_PLANE_URL", "http://control-plane:80").rstrip("/")
SERVICE_AUTH_TOKEN = os.environ.get("SERVICE_AUTH_TOKEN", "")

# ─── Template engine ─────────────────────────────────────────────────────

TEMPLATES = Environment(loader=FileSystemLoader("templates"))

# ─── Shared HTTP client ──────────────────────────────────────────────────

http_client: httpx.AsyncClient | None = None


def get_client() -> httpx.AsyncClient:
    assert http_client is not None, "http_client not initialised"
    return http_client


# ─── Helpers ─────────────────────────────────────────────────────────────

async def validate_session(cookie_value: str | None) -> dict | None:
    """Validate session cookie against the control plane.

    Returns the session dict (user_id, username, display_name, is_admin)
    or *None* if the cookie is missing / invalid.
    """
    if not cookie_value:
        return None
    try:
        resp = await get_client().get(
            f"{CONTROL_PLANE_URL}/api/session",
            headers={"Cookie": f"session={cookie_value}"},
        )
        if resp.status_code == 200:
            return resp.json()
    except httpx.HTTPError:
        pass
    return None


async def get_routing_status(workspace_id: str, username: str | None = None) -> dict | None:
    """Fetch internal routing status from the control plane.

    *username* (the session user) is forwarded as X-Service-User so the
    control plane can authorize the routing request.
    """
    headers = {"X-Service-Auth": SERVICE_AUTH_TOKEN}
    if username:
        headers["X-Service-User"] = username
    try:
        resp = await get_client().get(
            f"{CONTROL_PLANE_URL}/api/internal/workspaces/{workspace_id}/routing",
            headers=headers,
        )
        if resp.status_code == 200:
            return resp.json()
    except httpx.HTTPError:
        pass
    return None


async def get_mcp_target(workspace_id: str, server_id: str, username: str) -> dict | None:
    """Resolve an MCP server's port from the control plane (authorized)."""
    try:
        resp = await get_client().get(
            f"{CONTROL_PLANE_URL}/api/internal/workspaces/{workspace_id}/mcp/{server_id}",
            headers={"X-Service-Auth": SERVICE_AUTH_TOKEN, "X-Service-User": username},
        )
        if resp.status_code == 200:
            return resp.json()
    except httpx.HTTPError:
        pass
    return None


async def trigger_start(workspace_id: str, session_cookie: str | None = None) -> bool:
    """Idempotently start / resume a workspace.

    Uses a deterministic Idempotency-Key derived from the workspace ID
    so that retries within a short window are safe.
    Requires the user's session cookie for auth (the start endpoint is session-protected).
    """
    headers = {
        "Idempotency-Key": f"gateway-start-{workspace_id}",
        "Content-Type": "application/json",
    }
    if session_cookie:
        headers["Cookie"] = f"session={session_cookie}"
    try:
        resp = await get_client().post(
            f"{CONTROL_PLANE_URL}/api/workspaces/{workspace_id}/start",
            headers=headers,
            json={},
        )
        return resp.status_code in (200, 202)
    except httpx.HTTPError:
        return False


async def record_audit(event_type: str, metadata: dict | None = None) -> None:
    """Fire-and-forget audit event."""
    try:
        await get_client().post(
            f"{CONTROL_PLANE_URL}/api/audit",
            headers={
                "X-Service-Auth": SERVICE_AUTH_TOKEN,
                "Content-Type": "application/json",
            },
            json={"event_type": event_type, "metadata": metadata or {}},
        )
    except httpx.HTTPError:
        pass  # best-effort


def render_template(name: str, **kwargs) -> str:
    """Render a Jinja2 template."""
    try:
        tmpl = TEMPLATES.get_template(name)
    except TemplateNotFound:
        return f"<h1>Template {name} not found</h1>"
    return tmpl.render(**kwargs)


def get_session_cookie(request: Request) -> str | None:
    """Extract the *session* cookie value."""
    return request.cookies.get("session")


def workspace_id_for(username: str) -> str:
    return f"ws-{username}"


# ─── Proxy helpers ───────────────────────────────────────────────────────

def _build_upstream_headers(request: Request) -> dict:
    """Build headers to forward to the upstream, removing hop-by-hop headers.

    Adds X-Forwarded-* headers.
    """
    hop_by_hop = frozenset({
        "connection", "proxy-connection", "keep-alive", "upgrade",
    })
    headers = {}
    for name, value in request.headers.items():
        if name.lower() not in hop_by_hop:
            headers[name] = value

    headers["X-Forwarded-Host"] = request.headers.get("host", "")
    headers["X-Forwarded-Proto"] = request.url.scheme
    if request.client:
        headers["X-Forwarded-For"] = request.client.host
    return headers


def _rewrite_location(location: str, prefix: str, target_base: str) -> str:
    """Rewrite a Location header so the browser follows the gateway prefix.

    If the location is a relative path (starts with ``/``) and does *not*
    already carry the *prefix*, it is prepended.
    Absolute URLs pointing at the *target_base* are also rewritten.
    """
    if not location:
        return location
    # Already has the prefix → no rewriting needed
    if location.startswith(prefix):
        return location
    # Absolute URL pointing at the upstream base → rewrite to gateway prefix
    if target_base and location.startswith(target_base):
        rest = location[len(target_base):]
        if not rest.startswith("/"):
            rest = "/" + rest
        return f"{prefix}{rest}"
    # Relative path that does not carry the prefix yet
    if location.startswith("/"):
        return f"{prefix}{location}"
    # External URL or other → leave untouched
    return location


async def proxy_http(
    request: Request,
    target_base: str,
    strip_prefix: str | None = None,
    *,
    expose_prefix: str | None = None,
    rewrite_location_prefix: str | None = None,
) -> Response:
    """Stream an HTTP request upstream.

    Parameters
    ----------
    target_base
        Base URL of the upstream service (e.g. ``http://<pod-ip>:6767``).
    strip_prefix
        Path prefix to remove before forwarding (e.g. ``/workspace/chat``).
    expose_prefix
        The gateway URL prefix to expose in Location rewrites
        (defaults to *strip_prefix*).
    rewrite_location_prefix
        If set, rewrite ``Location`` response headers that point inside
        the upstream so they include this prefix.
    """
    path = request.url.path
    if strip_prefix and path.startswith(strip_prefix):
        path = path[len(strip_prefix):]
    if path and not path.startswith("/"):
        path = "/" + path

    query = request.url.query
    url = f"{target_base.rstrip('/')}{path}"
    if query:
        url += f"?{query}"

    headers = _build_upstream_headers(request)
    body = await request.body()

    upstream_req = get_client().build_request(
        method=request.method,
        url=url,
        headers=headers,
        content=body,
    )
    upstream_resp = await get_client().send(upstream_req, stream=True)

    headers = {
        key: value
        for key, value in upstream_resp.headers.items()
        if key.lower() not in {
            "connection", "keep-alive", "proxy-authenticate",
            "proxy-authorization", "te", "trailer",
            "content-length", "content-encoding",
        }
    }
    return StreamingResponse(
        upstream_resp.aiter_bytes(),
        status_code=upstream_resp.status_code,
        headers=headers,
        background=BackgroundTask(upstream_resp.aclose),
    )
    resp_headers = dict(upstream_resp.headers)
async def _relay_ws(client_ws: WebSocket, upstream_uri: str, auth_token: str | None = None) -> None:
    """Relay WebSocket frames bidirectionally.

    If *auth_token* is provided, it is sent as a ``Sec-WebSocket-Protocol``
    subprotocol (``paseo.bearer.<token>``) during the upstream WebSocket
    handshake to authenticate with the Paseo daemon.
    """
    try:
        kwargs = {}
        if auth_token:
            kwargs["subprotocols"] = [f"paseo.bearer.{auth_token}"]
        async with websockets.connect(upstream_uri, **kwargs) as upstream_ws:
            async def client_to_upstream():
                try:
                    while True:
                        msg = await client_ws.receive()
                        if msg["type"] == "websocket.receive":
                            if msg.get("text"):
                                await upstream_ws.send(msg["text"])
                            elif msg.get("bytes"):
                                await upstream_ws.send(msg["bytes"])
                        elif msg["type"] == "websocket.disconnect":
                            break
                except WebSocketDisconnect:
                    pass

            async def upstream_to_client():
                try:
                    first = True
                    async for msg in upstream_ws:
                        if first:
                            first = False
                            mtype = "text" if isinstance(msg, str) else "binary"
                            logger.info("[ws.relay] upstream first msg type=%s size=%d", mtype, len(msg) if isinstance(msg, (str, bytes)) else 0)
                        if isinstance(msg, str):
                            await client_ws.send_text(msg)
                        elif isinstance(msg, bytes):
                            await client_ws.send_bytes(msg)
                except websockets.ConnectionClosed:
                    pass

            await asyncio.gather(client_to_upstream(), upstream_to_client())
    except websockets.InvalidURI:
        logger.warning("[ws.relay] InvalidURI for %s", upstream_uri)
        await client_ws.close(1011)
    except OSError:
        logger.warning("[ws.relay] OSError for %s", upstream_uri)
        await client_ws.close(1011)
    except Exception:
        logger.exception("[ws.relay] Unexpected error")


async def resolve_workspace(
    username: str,
    session_cookie: str | None = None,
    workspace_id: str | None = None,
) -> tuple[dict | None, Response | None]:
    """Resolve workspace routing info for the given user.

    Returns ``(routing, immediate_response)`` — at most one is set.

    * If *routing* is not *None* the workspace is ready to proxy.
    * If *immediate_response* is set, return it to the caller directly
      (either a starting page, an error page, or a redirect).

    *workspace_id* defaults to the user's own workspace; pass it to route
    to a workspace shared with one of the user's groups.
    """
    if not workspace_id:
        workspace_id = workspace_id_for(username)

    routing = await get_routing_status(workspace_id, username)

    if routing is None:
        return None, HTMLResponse(
            content=render_template("error.html", message="Could not resolve workspace routing information."),
            status_code=503,
        )

    state = routing.get("state", "")
    agent_ready = routing.get("agent_ready", False)

    # Running and agent ready → proxy directly
    if state == "running" and agent_ready is True:
        return routing, None

    # Hibernated / requested / failed → trigger a start, show starting page
    if state in ("hibernated", "requested", "failed"):
        await trigger_start(workspace_id, session_cookie)
        return None, HTMLResponse(
            content=render_template("starting.html"),
            status_code=200,
        )

    # Starting → show starting page (browser auto-refreshes via meta refresh)
    if state == "starting":
        return None, HTMLResponse(
            content=render_template("starting.html"),
            status_code=200,
        )

    # Any terminal / unexpected state → error page
    return None, HTMLResponse(
        content=render_template(
            "error.html",
            message=f"Workspace is in '{state}' state and cannot be accessed.",
        ),
        status_code=503,
    )

async def _authenticate_workspace_request(
    request: Request,
) -> tuple[dict | None, Response | None]:
    """Validate session and return ``(session, error_response)``.

    At most one element is non-None.
    """
    cookie = get_session_cookie(request)
    if not cookie:
        return None, RedirectResponse(url="/ui/login")

    session = await validate_session(cookie)
    if session is None:
        return None, RedirectResponse(url="/ui/login")

    return session, None


# ─── Application lifecycle ───────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global http_client
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(3600.0, connect=10.0),
        limits=httpx.Limits(max_keepalive_connections=100, max_connections=200),
    ) as client:
        http_client = client
        yield


app = FastAPI(title="Agent Workspace Gateway", lifespan=lifespan)

@app.middleware("http")
async def path_proxy(request: Request, call_next):
    """Route workspace services by path prefix (/canvas/, /code/, /chat/)"""
    print(f"[SCOPE] type={request.scope.get('type')} upgrade={request.headers.get('upgrade','')} path={request.url.path}", flush=True)
    BASE = request.url.path
    port_map = {"/canvas": 8000, "/code": 8080, "/chat": 6767}
    upstream_port = None
    prefix = None
    if BASE == "/mcp" or BASE.startswith("/mcp/"):
        session = await validate_session(get_session_cookie(request))
        if not session:
            return RedirectResponse(url="/ui/login")
        ws_override = (
            request.headers.get("X-Workspace-Id")
            or request.cookies.get("workspace")
            or None
        )
        routing, ws_resp = await resolve_workspace(
            session["username"],
            get_session_cookie(request),
            workspace_id=ws_override,
        )
        if ws_resp:
            return ws_resp
        cluster_ip = routing.get("cluster_ip")
        if not cluster_ip:
            return HTMLResponse(
                content=render_template("error.html", message="Workspace has no network endpoint."),
                status_code=503,
            )
        parts = [p for p in BASE.split("/") if p]
        if len(parts) < 2:
            return HTMLResponse(
                content=render_template("error.html", message="Missing MCP server id."),
                status_code=404,
            )
        server_id = parts[1]
        workspace_id = routing.get("workspace_id") or workspace_id_for(session["username"])
        target = await get_mcp_target(workspace_id, server_id, session["username"])
        if not target:
            return HTMLResponse(
                content=render_template("error.html", message="MCP server not found."),
                status_code=404,
            )
        asyncio.ensure_future(record_audit("gateway.route_granted", {"route_class": "mcp", "server_id": server_id}))
        return await proxy_http(request, f"http://{cluster_ip}:{target['port']}", strip_prefix=f"/mcp/{server_id}")


    for p, port in port_map.items():
        if BASE == p or BASE.startswith(p + "/"):
            upstream_port = port
            prefix = p
            break
    if upstream_port:
        session = await validate_session(get_session_cookie(request))
        if not session:
            return RedirectResponse(url="/ui/login")
        ws_override = (
            request.headers.get("X-Workspace-Id")
            or request.cookies.get("workspace")
            or None
        )
        routing, ws_resp = await resolve_workspace(
            session["username"],
            get_session_cookie(request),
            workspace_id=ws_override,
        )
        if ws_resp:
            return ws_resp
        cluster_ip = routing.get("cluster_ip")
        if not cluster_ip:
            return HTMLResponse(
                content=render_template("error.html", message="Workspace has no network endpoint."),
                status_code=503,
            )
        asyncio.ensure_future(record_audit("gateway.route_granted", {"route_class": prefix.strip("/"), "via": "path"}))
        target = BASE[len(prefix):] if BASE.startswith(prefix) else BASE
        if not target.startswith("/"): target = "/" + target
        async with httpx.AsyncClient() as client:
            url = f"http://{cluster_ip}:{upstream_port}{target}"
            qs = request.url.query
            if qs: url += "?" + qs
            hdrs = _build_upstream_headers(request)
            body = await request.body()
            resp = await client.request(request.method, url, headers=hdrs, content=body)
            ct = resp.headers.get("content-type", "")
            if "text/html" in ct and resp.status_code == 200:
                html = resp.text
                # Rewrite root-relative asset paths to include prefix
                html = html.replace('href="/assets/', f'href="{prefix}/assets/')
                html = html.replace('src="/assets/', f'src="{prefix}/assets/')
                html = html.replace('href="/favicon', f'href="{prefix}/favicon')
                # Inject <base> tag for any remaining relative URLs
                base_tag = f'<base href="{prefix}/">\n'
                if "<head>" in html:
                    html = html.replace("<head>", f"<head>{base_tag}", 1)
                elif "</head>" in html:
                    html = html.replace("</head>", f"{base_tag}</head>", 1)
                new_h = dict(resp.headers)
                new_h.pop("content-length", None)
                return Response(content=html, status_code=200, headers=new_h)
            new_h = {k:v for k,v in resp.headers.items() if k.lower() not in {"content-length","content-encoding","transfer-encoding","date","server"}}
            return Response(content=resp.content, status_code=resp.status_code, headers=new_h)
    return await call_next(request)


@app.get("/")
async def root_redirect(request: Request):
    """Redirect root to workspaces if logged in, or login page if not."""
    session = await validate_session(get_session_cookie(request))
    if session:
        return RedirectResponse(url="/ui/workspaces")
    return RedirectResponse(url="/ui/login")

@app.get("/api/health")
async def health():
    return {"status": "ok", "service": "gateway"}


# ─── Auth routes (proxied to control plane) ──────────────────────────

@app.post("/api/login")
async def login_proxy(request: Request):
    """Proxy login to control plane — it sets the session cookie."""
    # Direct call to control-plane, bypass proxy_http cookie issues
    async with httpx.AsyncClient() as c:
        body = await request.body()
        headers = {
            "Cookie": f"session={get_session_cookie(request)}" if get_session_cookie(request) else "",
            "Content-Type": request.headers.get("content-type", "application/json"),
        }
        url = f"{CONTROL_PLANE_URL}{request.url.path}"
        if request.url.query:
            url += "?" + request.url.query
        method = request.method.lower()
        resp = await c.request(method, url, headers=headers, content=body)
        resp_headers = dict(resp.headers)
        # Add cookie domain so chat.* and code.* subdomains can read it
        set_cookie = resp_headers.get("set-cookie", "")
        if set_cookie and "domain=" not in set_cookie.lower():
            resp_headers["set-cookie"] = set_cookie + "; Domain=" + os.environ.get("COOKIE_DOMAIN", ".example.com")
        return Response(content=resp.content, status_code=resp.status_code, headers={k:v for k,v in resp_headers.items() if k.lower() not in ["content-length","content-encoding","transfer-encoding","date","server"]})


@app.post("/api/logout")
async def logout_proxy(request: Request):
    """Proxy logout to control plane."""
    return await proxy_http(request, CONTROL_PLANE_URL, strip_prefix="")


# ─── Login page (served directly) ────────────────────────────────────

@app.get("/ui/login")
async def login_page():
    """Serve the login form HTML."""
    html = render_template("login.html")
    return HTMLResponse(content=html)


# ─── UI pages (served directly by the gateway) ───────────────────────

@app.get("/ui/workspaces")
async def workspaces_page():
    """Serve the workspace manager HTML."""
    html = render_template("workspaces.html")
    return HTMLResponse(content=html)


@app.get("/ui/groups")
async def groups_page():
    """Serve the group management HTML."""
    html = render_template("groups.html")
    return HTMLResponse(content=html)


# ─── UI proxy — everything else under /ui/ goes to control plane ────



@app.api_route("/ui/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def ui_proxy(request: Request, path: str):
    """Proxy UI routes (except /ui/login) to the control plane."""
    return await proxy_http(request, CONTROL_PLANE_URL, strip_prefix="/ui")


# ─── Workspace status ────────────────────────────────────────────────

@app.get("/workspace/status")
async def workspace_status(request: Request):
    """Return user-safe workspace status JSON from control plane."""
    session, err_resp = await _authenticate_workspace_request(request)
    if err_resp:
        return err_resp

    workspace_id = workspace_id_for(session["username"])
    return await proxy_http(
        request,
        f"{CONTROL_PLANE_URL}/api/workspaces/{workspace_id}/status",
        strip_prefix="/workspace/status",
    )


@app.get("/workspace/status/{path:path}")
async def workspace_status_path(request: Request, path: str):
    """Same as above — catches sub-paths."""
    session, err_resp = await _authenticate_workspace_request(request)
    if err_resp:
        return err_resp

    workspace_id = workspace_id_for(session["username"])
    return await proxy_http(
        request,
        f"{CONTROL_PLANE_URL}/api/workspaces/{workspace_id}/status/{path}",
        strip_prefix="/workspace/status",
    )


# ─── Workspace chat (Paseo) ──────────────────────────────────────────



@app.get("/_expo/{path:path}")
async def paseo_assets(request: Request, path: str):
    """Proxy Paseo root assets to the current user's workspace."""
    return await resolve_and_proxy(request, f"/workspace/chat/{path}")

@app.get("/favicon.ico")
async def paseo_favicon(request: Request):
    """Proxy favicon to the current user's workspace Paseo."""
    return await resolve_and_proxy(request, "/workspace/chat/favicon.ico")

@app.get("/manifest.json")
async def paseo_manifest(request: Request):
    return await resolve_and_proxy(request, "/workspace/chat/manifest.json")

@app.get("/apple-touch-icon{rest:path}")
async def paseo_apple_icon(request: Request, rest: str):
    return await resolve_and_proxy(request, f"/workspace/chat/apple-touch-icon{rest}")

@app.get("/service-worker.js")
async def paseo_sw(request: Request):
    return await resolve_and_proxy(request, "/workspace/chat/service-worker.js")


async def resolve_and_proxy(request: Request, target_path: str) -> Response:
    """Resolve workspace and proxy request to Paseo."""
    session = await validate_session(get_session_cookie(request))
    if not session:
        return RedirectResponse(url="/ui/login")
    routing, ws_resp = await resolve_workspace(session.get("username", ""), get_session_cookie(request))
    if ws_resp:
        return ws_resp
    cluster_ip = routing.get("cluster_ip")
    if not cluster_ip:
        return HTMLResponse(content="Workspace unavailable", status_code=503)
    return await proxy_http(request, f"http://{cluster_ip}:6767", strip_prefix="")
@app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])

async def api_proxy(request: Request, path: str):
    """Proxy API routes to the control plane."""
    session = await validate_session(get_session_cookie(request))
    if not session and not path.startswith("health"):
        return RedirectResponse(url="/ui/login")
    # Direct call to avoid proxy cookie forwarding issues
    async with httpx.AsyncClient() as c:
        body = await request.body()
        ck = get_session_cookie(request)
        headers = {
            "Cookie": f"session={ck}" if ck else "",
            "Content-Type": request.headers.get("content-type", "application/json"),
        }
        url = f"{CONTROL_PLANE_URL}{request.url.path}"
        if request.url.query:
            url += "?" + request.url.query
        method = request.method.lower()
        resp = await c.request(method, url, headers=headers, content=body)
        resp_headers = dict(resp.headers)
        # Add cookie domain so chat.* and code.* subdomains can read it
        set_cookie = resp_headers.get("set-cookie", "")
        if set_cookie and "domain=" not in set_cookie.lower():
            resp_headers["set-cookie"] = set_cookie + "; Domain=" + os.environ.get("COOKIE_DOMAIN", ".example.com")
        return Response(content=resp.content, status_code=resp.status_code, headers={k:v for k,v in resp_headers.items() if k.lower() not in ["content-length","content-encoding","transfer-encoding","date","server"]})


async def _proxy_paseo(request: Request, cluster_ip: str) -> Response:
    """Proxy to Paseo, inject base tag + routing fix for Expo Router."""
    async with httpx.AsyncClient() as client:
        path = request.url.path
        if path.startswith("/workspace/chat"):
            path = path[len("/workspace/chat"):]
        if not path.startswith("/"):
            path = "/" + path
        query = request.url.query
        url = f"http://{cluster_ip}:6767{path}"
        if query:
            url += "?" + query
        headers = _build_upstream_headers(request)
        body = await request.body()
        resp = await client.request(request.method, url, headers=headers, content=body)
        ct = resp.headers.get("content-type", "")
        if "text/html" in ct and resp.status_code == 200:
            # Forward Paseo HTML as-is - bundled UI auto-connects to same origin
            return Response(content=resp.text, status_code=200, headers=dict(resp.headers))
            html = resp.text
            # inject <base> tag + JS routing fix before </head>
            fix = (
                '<base href="/workspace/chat/" />'
                '<script>'
                '(function(){'
                "var b='/workspace/chat';"
                "if(location.pathname===b||location.pathname===b+'/'){"
                "history.replaceState(null,'','/');"
                "}"
                "var p=history.pushState.bind(history);"
                "history.pushState=function(s,t,u){"
                "if(typeof u==='string'&&u.startsWith('/')&&!u.startsWith(b)){u=b+u;}"
                "return p(s,t,u);"
                "};"
                "})();"
                '</script>'
            )
            html = html.replace("</head>", fix + "</head>")
            new_headers = dict(resp.headers)
            new_headers.pop("content-length", None)
            return Response(content=html, status_code=200, headers=new_headers)
        return Response(content=resp.content, status_code=resp.status_code, headers={k:v for k,v in resp.headers.items() if k.lower() not in ["content-length","content-encoding","transfer-encoding","date","server"]})
@app.api_route("/workspace/chat/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
async def workspace_chat(request: Request, path: str):
    """Proxy HTTP requests to Paseo in the user workspace."""
    session, err_resp = await _authenticate_workspace_request(request)
    if err_resp:
        return err_resp

    routing, ws_resp = await resolve_workspace(session["username"], get_session_cookie(request))
    if ws_resp:
        return ws_resp

    cluster_ip = routing.get("cluster_ip")
    if not cluster_ip:
        return HTMLResponse(
            content=render_template("error.html", message="Workspace has no network endpoint."),
            status_code=503,
        )

    # Fire-and-forget audit
    asyncio.ensure_future(record_audit("gateway.route_granted", {"route_class": "chat"}))

    return await _proxy_paseo(request, cluster_ip)


@app.api_route("/workspace/chat", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
async def workspace_chat_root(request: Request):
    """Handle bare /workspace/chat."""
    return await workspace_chat(request, "")

@app.websocket("/workspace/chat/ws/{path:path}")
async def workspace_chat_ws(websocket: WebSocket, path: str):
    """Proxy WebSocket connections to Paseo."""
    await websocket.accept()

    cookie = websocket.cookies.get("session")
    if not cookie:
        await websocket.close(1008)
        return

    session = await validate_session(cookie)
    if session is None:
        await websocket.close(1008)
        return

    routing, ws_resp = await resolve_workspace(session["username"], websocket.cookies.get("session"))
    if ws_resp or not routing:
        await websocket.close(1011)
        return

    cluster_ip = routing.get("cluster_ip")
    if not cluster_ip:
        await websocket.close(1011)
        return

    asyncio.ensure_future(record_audit("gateway.route_granted", {"route_class": "chat", "protocol": "ws"}))

    # Fetch Paseo password for WebSocket auth
    paseo_ws_token = ""
    try:
        async with httpx.AsyncClient() as hc:
            pw_resp = await hc.get(f"http://{cluster_ip}:9000/password", timeout=httpx.Timeout(3.0))
            if pw_resp.status_code == 200:
                paseo_ws_token = pw_resp.json().get("password", "")
    except Exception:
        pass
    upstream_uri = f"ws://{cluster_ip}:6767/ws/{path}"
    await _relay_ws(websocket, upstream_uri, auth_token=paseo_ws_token or None)

@app.api_route("/workspace/code/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
async def workspace_code(request: Request, path: str):
    """Proxy HTTP requests to code-server in the user workspace.

    Redirect Location headers from code-server are rewritten to preserve
    the ``/workspace/code`` prefix so the browser follows them through
    the gateway.
    """
    session, err_resp = await _authenticate_workspace_request(request)
    if err_resp:
        return err_resp

    routing, ws_resp = await resolve_workspace(session["username"], get_session_cookie(request))
    if ws_resp:
        return ws_resp

    cluster_ip = routing.get("cluster_ip")
    if not cluster_ip:
        return HTMLResponse(
            content=render_template("error.html", message="Workspace has no network endpoint."),
            status_code=503,
        )

    asyncio.ensure_future(record_audit("gateway.route_granted", {"route_class": "code"}))

    return await proxy_http(
        request,
        f"http://{cluster_ip}:8080",
        strip_prefix="/workspace/code",
        rewrite_location_prefix="/workspace/code",
    )
@app.api_route("/workspace/code", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
async def workspace_code_root(request: Request):
    """Handle bare /workspace/code (no trailing slash)."""
    return await workspace_code(request, "")


@app.websocket("/workspace/code/{path:path}")
async def _code_ws_relay(websocket: WebSocket, path: str):
    """Proxy WebSocket connections to code-server."""
    await websocket.accept()

    cookie = websocket.cookies.get("session")
    if not cookie:
        await websocket.close(1008)
        return

    session = await validate_session(cookie)
    if session is None:
        await websocket.close(1008)
        return

    routing, ws_resp = await resolve_workspace(session["username"], websocket.cookies.get("session"))
    if ws_resp or not routing:
        await websocket.close(1011)
        return

    cluster_ip = routing.get("cluster_ip")
    if not cluster_ip:
        await websocket.close(1011)
        return

    asyncio.ensure_future(record_audit("gateway.route_granted", {"route_class": "code", "protocol": "ws"}))

    upstream_uri = f"ws://{cluster_ip}:8080/{path}"
    await _relay_ws(websocket, upstream_uri)


@app.websocket("/workspace/code")
async def workspace_code_ws_bare(websocket: WebSocket):
    """Bare /workspace/code WebSocket for code-server."""
    await _code_ws_relay(websocket, "")

@app.api_route("/workspace/dev/{port:int}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
async def workspace_dev_root(request: Request, port: int):
    """Handle bare /workspace/dev/<port> (no trailing path)."""
    return await workspace_dev(request, port, "")


@app.api_route("/workspace/dev/{port:int}/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
async def workspace_dev(request: Request, port: int, path: str):
    """Proxy HTTP requests to a registered dev server port.

    The port must be registered via the workspace agent's exposure API.
    Only allowlisted ports are accepted.
    """
    session, err_resp = await _authenticate_workspace_request(request)
    if err_resp:
        return err_resp

    routing, ws_resp = await resolve_workspace(session["username"], get_session_cookie(request))
    if ws_resp:
        return ws_resp

    cluster_ip = routing.get("cluster_ip")
    if not cluster_ip:
        return HTMLResponse(
            content=render_template("error.html", message="Workspace has no network endpoint."),
            status_code=503,
        )

    # Find the registered exposure for this port
    exposures = routing.get("exposures", [])
    matched = [e for e in exposures if e.get("port") == port]
    if not matched:
        return HTMLResponse(
            content=render_template("error.html", message=f"Port {port} is not registered. Use 'workspace expose {port}' to register it."),
            status_code=404,
        )

    exposure = matched[0]
    exposure_id = exposure.get("id", port)
    asyncio.ensure_future(record_audit("gateway.route_granted", {
        "route_class": "dev",
        "port": port,
        "exposure_id": str(exposure_id),
    }))

    return await proxy_http(
        request,
        f"http://{cluster_ip}:9000/agent/exposures/{exposure_id}/proxy",
        strip_prefix=f"/workspace/dev/{port}",
    )


@app.websocket("/workspace/dev/{port:int}/ws/{path:path}")
async def workspace_dev_ws(websocket: WebSocket, port: int, path: str):
    """Proxy WebSocket connections to a registered dev server."""
    await websocket.accept()

    cookie = websocket.cookies.get("session")
    if not cookie:
        await websocket.close(1008)
        return

    session = await validate_session(cookie)
    if session is None:
        await websocket.close(1008)
        return

    routing, ws_resp = await resolve_workspace(session["username"], websocket.cookies.get("session"))
    if ws_resp or not routing:
        await websocket.close(1011)
        return

    cluster_ip = routing.get("cluster_ip")
    if not cluster_ip:
        await websocket.close(1011)
        return

    exposures = routing.get("exposures", [])
    matched = [e for e in exposures if e.get("port") == port]
    if not matched:
        await websocket.close(1008)
        return

    exposure_id = matched[0].get("id", port)
    asyncio.ensure_future(record_audit("gateway.route_granted", {
        "route_class": "dev",
        "port": port,
        "protocol": "ws",
    }))

    upstream_uri = f"ws://{cluster_ip}:9000/agent/exposures/{exposure_id}/proxy/ws/{path}"
    await _relay_ws(websocket, upstream_uri)


@app.get("/workspace/starting")
async def workspace_starting():
    """Show the 'workspace is starting' page."""
    return HTMLResponse(content=render_template("starting.html"))


# ─── Catch-all for unmatched routes ─────────────────────────────────


@app.websocket("/{path:path}")
@app.websocket("/canvas/sockets")
@app.websocket("/chat/ws")
async def path_ws_root(websocket: WebSocket):
    """Handle WebSocket for workspace services."""
    path = websocket.url.path
    if path == "/canvas/sockets":
        await relay_ws(websocket, 8000)
    elif path == "/chat/ws":
        await relay_ws(websocket, 6767)
    else:
        await websocket.close(1011)
    async def client_to_upstream():
        try:
            while True:
                msg = await websocket.receive()
                if msg["type"] == "websocket.receive":
                    if msg.get("text"):
                        logger.warning("[ws.seq] t=%06d browser->upstream text len=%d", int((time.monotonic()-_t0)*1e6), len(msg["text"]))
                        await upstream_ws.send(msg["text"])
                    elif msg.get("bytes"):
                        await upstream_ws.send(msg["bytes"])
                elif msg["type"] == "websocket.disconnect":
                    break
        except WebSocketDisconnect:
            pass

    async def upstream_to_client():
        try:
            async for msg in upstream_ws:
                now = time.monotonic() - _t0
                mtype = "text" if isinstance(msg, str) else "binary"
                if isinstance(msg, str):
                    logger.warning("[ws.seq] t=%06d upstream->browser text len=%d first200=%s", int(now*1e6), len(msg), msg[:200])
                    await websocket.send_text(msg)
                elif isinstance(msg, bytes):
                    await websocket.send_bytes(msg)
        except websockets.ConnectionClosed:
            pass

    await asyncio.gather(client_to_upstream(), upstream_to_client())
    logger.warning("[ws.seq] t=%06d relay done", int((time.monotonic()-_t0)*1e6))
@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
async def catch_all(path: str):
    """Serve 404 page for any unmatched route."""
    return HTMLResponse(
        content=render_template("404.html"),
        status_code=404,
    )
