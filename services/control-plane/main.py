"""Control-plane FastAPI application.

Serves the /api/* endpoints from openapi.yaml and runs an async background
reconciler for K8s workspace resources.
"""

import json
import logging
import asyncio
import os
import re
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import delete, desc, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from audit import record_audit_event
from auth import (
    authenticate_user,
    create_session,
    delete_session_cookie,
    make_session_cookie,
    validate_session,
)
from database import async_session_factory, get_session, init_db
from idempotency import idempotency_store
from models import AuditEvent, Group, GroupMember, McpServer, Quota, Session, UsageEvent, User, Workspace, WorkspaceSecret, WorkspaceShare
from oidc import router as oidc_router
from reconciler import reconciler
from secrets_store import SecretDecryptionError, decrypt_value, encrypt_value
from schemas import (
    NetworkConfigOut,
    NetworkConfigUpdate,
    AddMemberRequest,
    AuditEventOut,
    AuditPage,
    CreateGroupRequest,
    Error,
    GroupDetail,
    GroupOut,
    HealthResponse,
    LoginRequest,
    LoginResponse,
    McpServerCreate,
    McpServerOut,
    McpServerUpdate,
    McpTargetOut,
    Ok,
    Operation,
    QuotaOut,
    QuotaUpdate,
    SECRET_KEY_RE,
    SecretOut,
    SecretUpsert,
    SecretValueOut,
    SessionOut,
    ShareOut,
    ShareRequest,
    USAGE_CATEGORIES,
    UsageIngestRequest,
    UsageIngestResponse,
    UsagePage,
    UsageSummaryOut,
    WorkspaceOut,
    WorkspaceRoutingStatus,
    WorkspaceStatus,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("control-plane")

SERVICE_AUTH_TOKEN = os.environ.get("SERVICE_AUTH_TOKEN")
if not SERVICE_AUTH_TOKEN:
    raise RuntimeError(
        "SERVICE_AUTH_TOKEN must be set: it authenticates gateway-originated "
        "internal calls (routing, MCP, audit). Pod-originated calls "
        "authenticate with per-workspace tokens (X-Workspace-Token) instead."
    )
CORRELATION_ID_HEADER = "X-Correlation-Id"
REQUEST_ID_HEADER = "X-Request-Id"
IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"

# Login throttling: bound credential-stuffing attempts per (client IP, username).
# In-memory (uvicorn single process, replicas: 1).
LOGIN_MAX_ATTEMPTS = 10
LOGIN_WINDOW_SECONDS = 300  # 5 minutes
LOGIN_ATTEMPT_STORE: dict[tuple[str, str], list[float]] = {}


def _login_attempt_throttled(client_ip: str, username: str) -> bool:
    """Record a login attempt; return True if the caller is over the limit.

    Every attempt is recorded; attempts older than LOGIN_WINDOW_SECONDS are
    pruned. Returns True once more than LOGIN_MAX_ATTEMPTS attempts remain in
    the window (i.e. the (LOGIN_MAX_ATTEMPTS + 1)-th attempt is rejected).
    """
    now = time.monotonic()
    key = (client_ip, username)
    attempts = [t for t in LOGIN_ATTEMPT_STORE.get(key, []) if now - t < LOGIN_WINDOW_SECONDS]
    attempts.append(now)
    LOGIN_ATTEMPT_STORE[key] = attempts
    return len(attempts) > LOGIN_MAX_ATTEMPTS


# ─── Lifespan ──────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: init DB, start reconciler. Shutdown: stop reconciler."""
    logger.info("Starting control-plane service")
    await init_db()

    # Seed test users if table is empty
    async with async_session_factory() as db:
        result = await db.execute(select(func.count()).select_from(User))
        count = result.scalar()
        if count == 0:
            from auth import hash_password
            # Seed default admin user if none exist (configure via env vars).
            # The username defaults to "admin", but the password is REQUIRED:
            # refusing to start with a well-known default credential.
            admin_username = os.environ.get("SEED_ADMIN_USER", "admin")
            admin_password = os.environ.get("SEED_ADMIN_PASSWORD")
            if not admin_password:
                raise RuntimeError(
                    "SEED_ADMIN_PASSWORD must be set when the users table is empty "
                    "(initial admin seeding); refusing to start with a default password."
                )
            admin = User(
                username=admin_username,
                password_hash=hash_password(admin_password),
                display_name="Admin",
                is_admin=True,
            )
            db.add(admin)
            logger.info(f"Seeded admin user: {admin_username}")
            await db.commit()
            logger.info("Seeded admin user")

    # Start background reconciler (skip when DISABLE_RECONCILER=1, e.g. the
    # Docker Compose local stack where there is no cluster to reconcile).
    reconciler_enabled = os.environ.get("DISABLE_RECONCILER", "").lower() not in ("1", "true", "yes")
    if reconciler_enabled:
        task = asyncio.create_task(reconciler.run())
        app.state.reconciler_task = task

    yield

    # Shutdown
    if reconciler_enabled:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    logger.info("Control-plane service stopped")




app = FastAPI(
    title="Agent Workspace Control Plane",
    version="0.2.0",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
)
app.include_router(oidc_router)

# CORS — origins are env-driven (CORS_ORIGINS, comma-separated). Never
# wildcard+credentials together (browsers reject it); with no origins
# configured, cross-origin requests are simply not allowed.
CORS_ORIGINS = [o.strip() for o in os.environ.get("CORS_ORIGINS", "").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=bool(CORS_ORIGINS),
    allow_methods=["*"],
    allow_headers=["*"],
)




# ─── Middleware: Session Auth ──────────────────────────────────────────

SESSION_COOKIE_NAME = "session"
SESSION_EXEMPT_PATHS = {"/api/login", "/api/health", "/api/oidc/login", "/api/oidc/callback", "/api/oidc/config"}


@app.middleware("http")
async def session_auth_middleware(request: Request, call_next):
    """Validate session cookie on /api/* requests.

    Sets request.state.user on success.
    Skips /api/login and /api/health.
    Internal endpoints (/api/internal/*, /api/audit) are handled by
    X-Service-Auth middleware instead.
    """
    path = request.url.path

    # Only apply to /api/* paths
    if not path.startswith("/api/"):
        return await call_next(request)

    # Exempt paths
    if path in SESSION_EXEMPT_PATHS:
        return await call_next(request)

    # Internal endpoints use X-Service-Auth, not session cookie
    if path.startswith("/api/internal/") or path == "/api/audit":
        return await call_next(request)

    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    if not session_id:
        return JSONResponse(status_code=401, content=Error(error="No session").model_dump())

    async with async_session_factory() as db:
        user = await validate_session(db, session_id)
        if user is None:
            return JSONResponse(status_code=401, content=Error(error="Invalid or expired session").model_dump())
        request.state.user = {
            "user_id": user.user_id,
            "username": user.username,
            "display_name": user.display_name,
            "is_admin": user.is_admin,
        }

    response = await call_next(request)
    return response


# ─── Middleware: X-Service-Auth (internal endpoints) ──────────────────

SERVICE_AUTH_EXEMPT_PATHS = {
    "/api/health",
    # Pod-originated: authenticated by the handler via X-Workspace-Token
    # (per-workspace secret), not the shared gateway X-Service-Auth token.
    "/api/internal/usage",
}


@app.middleware("http")
async def service_auth_middleware(request: Request, call_next):
    """Validate X-Service-Auth on gateway-originated internal endpoints.

    Routing (/api/internal/workspaces/*), MCP, and audit calls come from the
    gateway and present the shared X-Service-Auth token. Usage ingestion is
    pod-originated and exempt: it authenticates per-workspace via
    X-Workspace-Token inside the handler.
    """
    path = request.url.path

    if not path.startswith("/api/"):
        return await call_next(request)

    if path in SERVICE_AUTH_EXEMPT_PATHS:
        return await call_next(request)

    if not (path.startswith("/api/internal/") or path == "/api/audit"):
        return await call_next(request)

    token = request.headers.get("X-Service-Auth", "")
    if not token or token != SERVICE_AUTH_TOKEN:
        return JSONResponse(status_code=403, content=Error(error="Invalid service auth").model_dump())

    # For internal endpoints, set a service identity
    request.state.service_identity = {
        "service": "internal",
        "actor_user_id": None,  # Internal services don't have a user_id
    }
    if path == "/api/audit":
        # The gateway can identify itself; actor_user_id stays None for system events
        pass

    return await call_next(request)


# ─── Helpers ───────────────────────────────────────────────────────────

def _get_correlation_id(request: Request) -> str:
    return request.headers.get(CORRELATION_ID_HEADER, "")


def _get_request_id(request: Request) -> str:
    return request.headers.get(REQUEST_ID_HEADER, str(uuid.uuid4()))


def _get_source_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""


def _get_user_id(request: Request) -> Optional[str]:
    user = getattr(request.state, "user", None)
    if user:
        return user["user_id"]
    return None


def _check_admin(request: Request) -> bool:
    user = getattr(request.state, "user", None)
    return user is not None and user.get("is_admin", False)


async def _get_workspace_for_user(request: Request, workspace_id: str, db: AsyncSession) -> Optional[Workspace]:
    """Get workspace if it belongs to the current user, user is admin,
    or the user is a member of a group the workspace is shared with."""
    user_id = _get_user_id(request)
    is_admin = _check_admin(request)
    result = await db.execute(select(Workspace).where(Workspace.workspace_id == workspace_id))
    ws = result.scalar_one_or_none()
    if ws is None:
        return None
    if is_admin or ws.user_id == user_id:
        return ws
    if await _user_has_share(db, user_id, workspace_id, "view"):
        return ws
    return None


async def _user_has_share(db: AsyncSession, user_id: str, workspace_id: str, required: str) -> bool:
    """True if *user_id* is a member of a group holding a share on the workspace.

    ``required`` is "view" or "operate"; operate is a superset of view.
    """
    result = await db.execute(
        select(WorkspaceShare.permission)
        .join(GroupMember, GroupMember.group_id == WorkspaceShare.group_id)
        .where(
            GroupMember.user_id == user_id,
            WorkspaceShare.workspace_id == workspace_id,
        )
    )
    perms = result.scalars().all()
    if required == "operate":
        return any(p == "operate" for p in perms)
    return any(p in ("view", "operate") for p in perms)


async def _can_operate_workspace(request: Request, ws: Workspace, db: AsyncSession) -> bool:
    """True if the caller may start/hibernate the workspace (owner, admin,
    or member of a group with operate permission)."""
    user_id = _get_user_id(request)
    if _check_admin(request) or ws.user_id == user_id:
        return True
    return await _user_has_share(db, user_id, ws.workspace_id, "operate")


async def _service_user_can_access(db: AsyncSession, ws: Workspace, user: User) -> bool:
    """True if a gateway-identified user may access *ws* via the internal API.

    Shared by the routing and activity endpoints: owner, admin, or member of
    a group holding operate permission. Anything else is a 404 (no existence
    leak), mirroring the routing endpoint.
    """
    return (
        user.is_admin
        or ws.user_id == user.user_id
        or await _user_has_share(db, user.user_id, ws.workspace_id, "operate")
    )


async def _group_member_role(db: AsyncSession, group_id: str, user_id: str) -> Optional[str]:
    """Return the caller's role in a group, or None if not a member."""
    result = await db.execute(
        select(GroupMember.role).where(
            GroupMember.group_id == group_id,
            GroupMember.user_id == user_id,
        )
    )
    return result.scalar_one_or_none()


async def _workspace_to_out(ws: Workspace, db: AsyncSession) -> WorkspaceOut:
    """Convert Workspace ORM to WorkspaceOut, fetching username."""
    result = await db.execute(select(User).where(User.user_id == ws.user_id))
    user = result.scalar_one_or_none()
    return WorkspaceOut(
        workspace_id=ws.workspace_id,
        user_id=ws.user_id,
        username=user.username if user else None,
        state=ws.state,
        image=ws.image,
        created_at=ws.created_at,
        started_at=ws.started_at,
        last_activity_at=ws.last_activity_at,
        idle_timeout_minutes=ws.idle_timeout_minutes,
    )


# ─── Auth endpoints ───────────────────────────────────────────────────

@app.post("/api/login")
async def login(request: Request):
    body = await request.json()
    login_data = LoginRequest(**body)

    # Login throttling: max LOGIN_MAX_ATTEMPTS per (client IP, username)
    # per LOGIN_WINDOW_SECONDS; every attempt is recorded.
    if _login_attempt_throttled(_get_source_ip(request), login_data.username):
        return JSONResponse(
            status_code=429,
            content=Error(error="Too many login attempts; try again later").model_dump(),
        )
    async with async_session_factory() as db:
        user = await authenticate_user(db, login_data.username, login_data.password)
        if user is None:
            await record_audit_event(
                db, "auth.login_failed",
                source_ip=_get_source_ip(request),
                request_id=_get_request_id(request),
                correlation_id=_get_correlation_id(request),
                metadata={"username": login_data.username},
            )
            await db.commit()
            return JSONResponse(
                status_code=401,
                content=Error(error="Invalid credentials").model_dump(),
            )

        session = await create_session(db, user)
        await record_audit_event(
            db, "auth.login_succeeded",
            actor_user_id=user.user_id,
            source_ip=_get_source_ip(request),
            request_id=_get_request_id(request),
            correlation_id=_get_correlation_id(request),
        )
        await db.commit()

        # Auto-create workspace if it doesn't exist
        result = await db.execute(select(Workspace).where(Workspace.user_id == user.user_id))
        ws = result.scalar_one_or_none()
        if ws is None:
            ws_id = f"ws-{user.username}"
            ws = Workspace(
                workspace_id=ws_id,
                user_id=user.user_id,
                state="requested",
                image="",
            )
            db.add(ws)
            await record_audit_event(
                db, "workspace.created",
                actor_user_id=user.user_id,
                workspace_id=ws_id,
                source_ip=_get_source_ip(request),
                request_id=_get_request_id(request),
                correlation_id=_get_correlation_id(request),
            )
            await db.commit()

        resp = JSONResponse(
            content=LoginResponse(
                user_id=user.user_id,
                username=user.username,
                display_name=user.display_name,
                is_admin=user.is_admin,
            ).model_dump(),
        )
        cookie_params = make_session_cookie(session.session_id)
        resp.set_cookie(**cookie_params)
        return resp


@app.post("/api/logout")
async def logout(request: Request):
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    if session_id:
        async with async_session_factory() as db:
            await db.execute(delete(Session).where(Session.session_id == session_id))
            await record_audit_event(
                db, "auth.logout",
                actor_user_id=_get_user_id(request),
                source_ip=_get_source_ip(request),
                request_id=_get_request_id(request),
                correlation_id=_get_correlation_id(request),
            )
            await db.commit()
    resp = JSONResponse(content=Ok().model_dump())
    cookie_params = delete_session_cookie()
    resp.set_cookie(**cookie_params)
    return resp


@app.get("/api/session")
async def get_session(request: Request):
    user = getattr(request.state, "user", None)
    if user is None:
        return JSONResponse(status_code=401, content=Error(error="No valid session").model_dump())
    async with async_session_factory() as db:
        session_id = request.cookies.get(SESSION_COOKIE_NAME, "")
        result = await db.execute(
            select(Session).where(Session.session_id == session_id)
        )
        session = result.scalar_one_or_none()
        return SessionOut(
            user_id=user["user_id"],
            username=user["username"],
            display_name=user["display_name"],
            is_admin=user["is_admin"],
            session_id=session.session_id if session else None,
            created_at=session.created_at if session else None,
            expires_at=session.expires_at if session else None,
        ).model_dump()


# ─── Workspace endpoints ──────────────────────────────────────────────

@app.get("/api/workspaces")
async def list_workspaces(request: Request):
    """List the caller's own workspace plus any shared with their groups."""
    user_id = _get_user_id(request)
    async with async_session_factory() as db:
        result = await db.execute(
            select(Workspace).where(Workspace.user_id == user_id)
        )
        workspaces = list(result.scalars().all())

        shared = await db.execute(
            select(Workspace)
            .join(WorkspaceShare, WorkspaceShare.workspace_id == Workspace.workspace_id)
            .join(GroupMember, GroupMember.group_id == WorkspaceShare.group_id)
            .where(GroupMember.user_id == user_id)
        )
        seen = {w.workspace_id for w in workspaces}
        for w in shared.scalars().all():
            if w.workspace_id not in seen:
                workspaces.append(w)
                seen.add(w.workspace_id)

        return [await _workspace_to_out(w, db) for w in workspaces]


@app.get("/api/workspaces/{workspace_id}")
async def get_workspace(request: Request, workspace_id: str):
    async with async_session_factory() as db:
        ws = await _get_workspace_for_user(request, workspace_id, db)
        if ws is None:
            return JSONResponse(status_code=404, content=Error(error="Not found").model_dump())
        return (await _workspace_to_out(ws, db)).model_dump()


@app.post("/api/workspaces/{workspace_id}/start")
async def start_workspace(request: Request, workspace_id: str):
    idempotency_key = request.headers.get(IDEMPOTENCY_KEY_HEADER, "")
    endpoint = "start"
    body = await request.body()

    async with async_session_factory() as db:
        # Authorize BEFORE consulting the idempotency cache: a cached 200/409
        # must never be served to a caller who lacks access.
        ws = await _get_workspace_for_user(request, workspace_id, db)
        if ws is None:
            return JSONResponse(status_code=404, content=Error(error="Not found").model_dump())

        # View access is not enough — starting mutates the workspace.
        if not await _can_operate_workspace(request, ws, db):
            return JSONResponse(status_code=403, content=Error(error="Insufficient permission").model_dump())

        # Idempotency check (after authz). The cache key includes the caller's
        # user_id, so a replay of another user's key can never surface their
        # cached response.
        user_id = _get_user_id(request) or ""
        if idempotency_key:
            cached = idempotency_store.get(idempotency_key, endpoint, workspace_id, user_id, body)
            if cached is not None:
                status, cached_body = cached
                return Response(content=cached_body, status_code=status, media_type="application/json")
            if idempotency_store.check_conflict(idempotency_key, endpoint, workspace_id, user_id, body):
                return JSONResponse(status_code=409, content=Error(error="Idempotency key conflict: different request body").model_dump())

        # Reject deleting/deleted
        if ws.state in ("deleting", "deleted"):
            resp = JSONResponse(status_code=409, content=Error(error=f"Cannot start workspace in state '{ws.state}'").model_dump())
            if idempotency_key:
                idempotency_store.set(idempotency_key, endpoint, workspace_id, user_id, body, 409, resp.body.decode())
            return resp

        # No-op if already running or starting
        if ws.state in ("running", "starting"):
            out = await _workspace_to_out(ws, db)
            resp = JSONResponse(status_code=200, content=out.model_dump(mode="json"))
            if idempotency_key:
                idempotency_store.set(idempotency_key, endpoint, workspace_id, user_id, body, 200, resp.body.decode())
            return resp

        # Set default image if empty
        if not ws.image:
            from reconciler import WORKSPACE_IMAGE
            ws.image = WORKSPACE_IMAGE

        # Transition to starting (started_at is persisted so the reconciler's
        # starting-deadline has a reference point). The UPDATE is conditional
        # on fewer than MAX_CONCURRENT_STARTS OTHER workspaces being
        # 'starting': the count is evaluated atomically inside the UPDATE, so
        # concurrent starts can never oversubscribe capacity (no
        # check-then-update race). A 0-row result means capacity is full.
        from reconciler import MAX_CONCURRENT_STARTS
        operation_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        result = await db.execute(
            update(Workspace)
            .where(Workspace.workspace_id == workspace_id)
            .where(
                select(func.count())
                .select_from(Workspace)
                .where(
                    Workspace.state == "starting",
                    Workspace.workspace_id != workspace_id,
                )
                .scalar_subquery()
                < MAX_CONCURRENT_STARTS
            )
            .values(state="starting", started_at=now)
        )
        if result.rowcount == 0:
            # Capacity full. Deliberately NOT stored under the idempotency
            # key: capacity is transient, so a retry after a slot frees must
            # be allowed to succeed rather than replay a cached 503.
            return JSONResponse(
                status_code=503,
                content=Error(error="Starting capacity reached; try again shortly").model_dump(),
            )
        await record_audit_event(
            db, "workspace.start_requested",
            actor_user_id=_get_user_id(request),
            workspace_id=workspace_id,
            request_id=_get_request_id(request),
            correlation_id=_get_correlation_id(request),
            source_ip=_get_source_ip(request),
        )
        await db.commit()

        result = Operation(
            workspace_id=workspace_id,
            state="starting",
            operation_id=operation_id,
            requested_at=now,
            correlation_id=_get_correlation_id(request) or None,
        )
        resp = JSONResponse(status_code=202, content=result.model_dump(mode="json"))
        if idempotency_key:
            idempotency_store.set(idempotency_key, endpoint, workspace_id, user_id, body, 202, resp.body.decode())
        return resp


@app.post("/api/workspaces/{workspace_id}/hibernate")
async def hibernate_workspace(request: Request, workspace_id: str):
    idempotency_key = request.headers.get(IDEMPOTENCY_KEY_HEADER, "")
    endpoint = "hibernate"
    body = await request.body()

    async with async_session_factory() as db:
        # Authorize BEFORE consulting the idempotency cache (see start).
        ws = await _get_workspace_for_user(request, workspace_id, db)
        if ws is None:
            return JSONResponse(status_code=404, content=Error(error="Not found").model_dump())

        # View access is not enough — hibernating mutates the workspace.
        if not await _can_operate_workspace(request, ws, db):
            return JSONResponse(status_code=403, content=Error(error="Insufficient permission").model_dump())

        # Idempotency check (after authz; cache key is caller-scoped).
        user_id = _get_user_id(request) or ""
        if idempotency_key:
            cached = idempotency_store.get(idempotency_key, endpoint, workspace_id, user_id, body)
            if cached is not None:
                status, cached_body = cached
                return Response(content=cached_body, status_code=status, media_type="application/json")
            if idempotency_store.check_conflict(idempotency_key, endpoint, workspace_id, user_id, body):
                return JSONResponse(status_code=409, content=Error(error="Idempotency key conflict: different request body").model_dump())

        if ws.state not in ("running", "idle_pending", "hibernating"):
            return JSONResponse(status_code=409, content=Error(error=f"Cannot hibernate workspace in state '{ws.state}'").model_dump())

        operation_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        await db.execute(
            update(Workspace)
            .where(Workspace.workspace_id == workspace_id)
            .values(state="hibernating")
        )
        await record_audit_event(
            db, "workspace.hibernate_requested",
            actor_user_id=_get_user_id(request),
            workspace_id=workspace_id,
            request_id=_get_request_id(request),
            correlation_id=_get_correlation_id(request),
            source_ip=_get_source_ip(request),
        )
        await db.commit()

        result = Operation(
            workspace_id=workspace_id,
            state="hibernating",
            operation_id=operation_id,
            requested_at=now,
            correlation_id=_get_correlation_id(request) or None,
        )
        resp = JSONResponse(status_code=202, content=result.model_dump(mode="json"))
        if idempotency_key:
            idempotency_store.set(idempotency_key, endpoint, workspace_id, user_id, body, 202, resp.body.decode())
        return resp


@app.get("/api/workspaces/{workspace_id}/status")
async def get_workspace_status(request: Request, workspace_id: str):
    async with async_session_factory() as db:
        ws = await _get_workspace_for_user(request, workspace_id, db)
        if ws is None:
            return JSONResponse(status_code=404, content=Error(error="Not found").model_dump())

        # Build user-safe status
        services: dict[str, str] = {}
        if ws.state == "running":
            services["paseo"] = "ready"
            services["code_server"] = "ready"
        elif ws.state in ("starting", "requested"):
            services["paseo"] = "starting"
            services["code_server"] = "starting"
        elif ws.state == "hibernated":
            services["paseo"] = "unreachable"
            services["code_server"] = "unreachable"
        else:
            services["paseo"] = "unreachable"
            services["code_server"] = "unreachable"

        return WorkspaceStatus(
            workspace_id=workspace_id,
            state=ws.state,
            services=services,
            exposures=[],
            last_activity_at=ws.last_activity_at,
        ).model_dump()


# ─── Internal routing (for gateway) ──────────────────────────────────

@app.get("/api/internal/workspaces/{workspace_id}/routing")
async def get_workspace_routing(request: Request, workspace_id: str):
    """Return routing info for the gateway.

    The gateway identifies the end user via X-Service-User; routing is only
    granted to the workspace owner, admins, or members of a group holding
    operate permission — anything else is a 404 (no existence leak).
    """
    service_user = request.headers.get("X-Service-User", "")
    if not service_user:
        return JSONResponse(status_code=401, content=Error(error="Missing X-Service-User").model_dump())

    async with async_session_factory() as db:
        result = await db.execute(
            select(Workspace).where(Workspace.workspace_id == workspace_id)
        )
        ws = result.scalar_one_or_none()
        if ws is None:
            return JSONResponse(status_code=404, content=Error(error="Not found").model_dump())

        user_result = await db.execute(select(User).where(User.username == service_user))
        user = user_result.scalar_one_or_none()
        if user is None:
            return JSONResponse(status_code=404, content=Error(error="Not found").model_dump())

        if not await _service_user_can_access(db, ws, user):
            return JSONResponse(status_code=404, content=Error(error="Not found").model_dump())

        # Dev mode (Docker Compose local stack): route to a fixed workspace
        # container instead of a K8s ClusterIP.
        dev_host = os.environ.get("WORKSPACE_DEV_HOST", "")
        if dev_host:
            cluster_ip = dev_host
            agent_ready = await reconciler._check_pod_ready_host(dev_host)
        else:
            cluster_ip = await reconciler._get_cluster_ip(ws.user_id)
            agent_ready = await reconciler._check_pod_ready(ws.user_id)

        return WorkspaceRoutingStatus(
            workspace_id=workspace_id,
            state=ws.state,
            cluster_ip=cluster_ip,
            agent_ready=agent_ready,
            exposures=[],
            agent_token=ws.agent_token or "",
        ).model_dump()


@app.post("/api/internal/activity")
async def record_workspace_activity(request: Request):
    """Record end-user activity on a workspace (gateway-originated).

    The gateway reports activity (throttled to >=60s per workspace) so the
    control plane can extend idle-timeout windows. Same ACL as routing
    (owner / admin / operate-share, via X-Service-User); anything else is a
    404 so the endpoint never leaks whether a workspace exists.
    """
    body = await request.json()
    workspace_id = body.get("workspace_id", "")
    if not workspace_id:
        return JSONResponse(status_code=422, content=Error(error="Missing workspace_id").model_dump())

    service_user = request.headers.get("X-Service-User", "")
    if not service_user:
        return JSONResponse(status_code=401, content=Error(error="Missing X-Service-User").model_dump())

    async with async_session_factory() as db:
        result = await db.execute(
            select(Workspace).where(Workspace.workspace_id == workspace_id)
        )
        ws = result.scalar_one_or_none()
        if ws is None:
            return JSONResponse(status_code=404, content=Error(error="Not found").model_dump())

        user_result = await db.execute(select(User).where(User.username == service_user))
        user = user_result.scalar_one_or_none()
        if user is None:
            return JSONResponse(status_code=404, content=Error(error="Not found").model_dump())

        if not await _service_user_can_access(db, ws, user):
            return JSONResponse(status_code=404, content=Error(error="Not found").model_dump())

        await db.execute(
            update(Workspace)
            .where(Workspace.workspace_id == workspace_id)
            .values(last_activity_at=datetime.now(timezone.utc))
        )
        await db.commit()
        return Response(status_code=204)


@app.get("/api/internal/workspaces/{workspace_id}/mcp/{server_id}")
async def get_mcp_target(request: Request, workspace_id: str, server_id: str):
    """Resolve an MCP server's port for the gateway.

    Same authorization as routing (owner / admin / operate share); a
    disabled or foreign server is a 404 (no existence leak). The gateway
    combines the returned port with the workspace's cluster IP.
    """
    service_user = request.headers.get("X-Service-User", "")
    if not service_user:
        return JSONResponse(status_code=401, content=Error(error="Missing X-Service-User").model_dump())

    async with async_session_factory() as db:
        result = await db.execute(select(Workspace).where(Workspace.workspace_id == workspace_id))
        ws = result.scalar_one_or_none()
        if ws is None:
            return JSONResponse(status_code=404, content=Error(error="Not found").model_dump())

        user_result = await db.execute(select(User).where(User.username == service_user))
        user = user_result.scalar_one_or_none()
        if user is None:
            return JSONResponse(status_code=404, content=Error(error="Not found").model_dump())

        allowed = (
            user.is_admin
            or ws.user_id == user.user_id
            or await _user_has_share(db, user.user_id, workspace_id, "operate")
        )
        if not allowed:
            return JSONResponse(status_code=404, content=Error(error="Not found").model_dump())

        server = await db.get(McpServer, server_id)
        if server is None or server.workspace_id != workspace_id or not server.enabled:
            return JSONResponse(status_code=404, content=Error(error="Not found").model_dump())

        return McpTargetOut(
            workspace_id=workspace_id,
            server_id=server_id,
            name=server.name,
            port=server.port,
            enabled=server.enabled,
        ).model_dump()



# ─── Admin endpoints ──────────────────────────────────────────────────

@app.get("/api/admin/workspaces")
async def admin_list_workspaces(request: Request):
    if not _check_admin(request):
        return JSONResponse(status_code=403, content=Error(error="Admin only").model_dump())
    async with async_session_factory() as db:
        result = await db.execute(select(Workspace).order_by(Workspace.created_at.desc()))
        workspaces = result.scalars().all()
        return [await _workspace_to_out(ws, db) for ws in workspaces]


@app.delete("/api/admin/workspaces/{workspace_id}")
async def admin_delete_workspace(request: Request, workspace_id: str, preserve_pvc: bool = False):
    if not _check_admin(request):
        return JSONResponse(status_code=403, content=Error(error="Admin only").model_dump())
    async with async_session_factory() as db:
        result = await db.execute(
            select(Workspace).where(Workspace.workspace_id == workspace_id)
        )
        ws = result.scalar_one_or_none()
        if ws is None:
            return JSONResponse(status_code=404, content=Error(error="Not found").model_dump())

        if ws.state == "deleted":
            return JSONResponse(status_code=409, content=Error(error="Workspace already deleted").model_dump())

        operation_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        await db.execute(
            update(Workspace)
            .where(Workspace.workspace_id == workspace_id)
            .values(state="deleting", preserve_pvc=preserve_pvc)
        )

        # The reconciler reads ws.preserve_pvc to decide whether the PVC /
        # namespace are torn down or kept for manual recovery.

        await record_audit_event(
            db, "admin.action",
            actor_user_id=_get_user_id(request),
            workspace_id=workspace_id,
            request_id=_get_request_id(request),
            correlation_id=_get_correlation_id(request),
            source_ip=_get_source_ip(request),
            metadata={"action": "delete_workspace", "preserve_pvc": preserve_pvc},
        )
        await db.commit()

        return JSONResponse(status_code=202, content={
            "workspace_id": workspace_id,
            "state": "deleting",
            "operation_id": operation_id,
        })


@app.get("/api/admin/audit")
async def admin_list_audit(
    request: Request,
    limit: int = 100,
    offset: int = 0,
    event_type: Optional[str] = None,
    actor_user_id: Optional[str] = None,
    workspace_id: Optional[str] = None,
):
    if not _check_admin(request):
        return JSONResponse(status_code=403, content=Error(error="Admin only").model_dump())
    async with async_session_factory() as db:
        query = select(AuditEvent)
        if event_type:
            query = query.where(AuditEvent.event_type == event_type)
        if actor_user_id:
            query = query.where(AuditEvent.actor_user_id == actor_user_id)
        if workspace_id:
            query = query.where(AuditEvent.workspace_id == workspace_id)

        count_query = select(func.count()).select_from(query.subquery())
        total_result = await db.execute(count_query)
        total = total_result.scalar() or 0

        query = query.order_by(desc(AuditEvent.timestamp)).limit(limit).offset(offset)
        result = await db.execute(query)
        events = result.scalars().all()

        return AuditPage(
            events=[
                AuditEventOut(
                    timestamp=e.timestamp,
                    event_type=e.event_type,
                    actor_user_id=e.actor_user_id,
                    workspace_id=e.workspace_id,
                    request_id=e.request_id,
                    correlation_id=e.correlation_id,
                    source_ip=e.source_ip,
                    metadata=e.metadata_,
                )
                for e in events
            ],
            total=total,
            limit=limit,
            offset=offset,
        ).model_dump()


@app.get("/api/admin/users")
async def admin_list_users(request: Request):
    if not _check_admin(request):
        return JSONResponse(status_code=403, content=Error(error="Admin only").model_dump())
    async with async_session_factory() as db:
        result = await db.execute(select(User).order_by(User.created_at))
        users = result.scalars().all()
        return [
            {
                "user_id": u.user_id,
                "username": u.username,
                "display_name": u.display_name,
                "is_admin": u.is_admin,
                "created_at": u.created_at,
                "disabled_at": u.disabled_at,
            }
            for u in users
        ]


# ─── Group endpoints ─────────────────────────────────────────────────

@app.post("/api/groups", status_code=201)
async def create_group(request: Request):
    body = await request.json()
    data = CreateGroupRequest(**body)
    user_id = _get_user_id(request)
    async with async_session_factory() as db:
        result = await db.execute(select(Group).where(Group.name == data.name))
        if result.scalar_one_or_none() is not None:
            return JSONResponse(status_code=409, content=Error(error="Group name already exists").model_dump())

        group_id = f"grp-{uuid.uuid4()}"
        group = Group(group_id=group_id, name=data.name, created_by=user_id)
        db.add(group)
        db.add(GroupMember(group_id=group_id, user_id=user_id, role="admin"))
        await record_audit_event(
            db, "group.created",
            actor_user_id=user_id,
            request_id=_get_request_id(request),
            correlation_id=_get_correlation_id(request),
            source_ip=_get_source_ip(request),
            metadata={"group_id": group_id, "name": data.name},
        )
        await db.commit()
        return GroupOut(group_id=group_id, name=group.name, created_at=group.created_at, role="admin").model_dump()


@app.get("/api/groups")
async def list_groups(request: Request):
    user_id = _get_user_id(request)
    async with async_session_factory() as db:
        result = await db.execute(
            select(Group, GroupMember.role)
            .join(GroupMember, GroupMember.group_id == Group.group_id)
            .where(GroupMember.user_id == user_id)
            .order_by(Group.created_at.desc())
        )
        return [
            GroupOut(group_id=g.group_id, name=g.name, created_at=g.created_at, role=role).model_dump()
            for g, role in result.all()
        ]


@app.get("/api/groups/{group_id}")
async def get_group(request: Request, group_id: str):
    user_id = _get_user_id(request)
    async with async_session_factory() as db:
        group = await db.get(Group, group_id)
        if group is None:
            return JSONResponse(status_code=404, content=Error(error="Not found").model_dump())
        if await _group_member_role(db, group_id, user_id) is None:
            return JSONResponse(status_code=404, content=Error(error="Not found").model_dump())

        members_result = await db.execute(
            select(GroupMember, User.username, User.display_name)
            .join(User, User.user_id == GroupMember.user_id)
            .where(GroupMember.group_id == group_id)
            .order_by(GroupMember.joined_at)
        )
        members = [
            {
                "user_id": m.user_id,
                "username": username,
                "display_name": display_name,
                "role": m.role,
                "joined_at": m.joined_at,
            }
            for m, username, display_name in members_result.all()
        ]
        return GroupDetail(
            group_id=group.group_id,
            name=group.name,
            created_at=group.created_at,
            members=members,
        ).model_dump()


@app.post("/api/groups/{group_id}/members", status_code=201)
async def add_group_member(request: Request, group_id: str):
    body = await request.json()
    data = AddMemberRequest(**body)
    user_id = _get_user_id(request)
    role = data.role if data.role in ("admin", "member") else "member"
    async with async_session_factory() as db:
        group = await db.get(Group, group_id)
        if group is None:
            return JSONResponse(status_code=404, content=Error(error="Not found").model_dump())
        if await _group_member_role(db, group_id, user_id) != "admin":
            return JSONResponse(status_code=403, content=Error(error="Group admin only").model_dump())

        user_result = await db.execute(select(User).where(User.username == data.username))
        user = user_result.scalar_one_or_none()
        if user is None:
            return JSONResponse(status_code=404, content=Error(error="User not found").model_dump())
        if await _group_member_role(db, group_id, user.user_id) is not None:
            return JSONResponse(status_code=409, content=Error(error="User is already a member").model_dump())

        db.add(GroupMember(group_id=group_id, user_id=user.user_id, role=role))
        await record_audit_event(
            db, "group.member_added",
            actor_user_id=user_id,
            request_id=_get_request_id(request),
            correlation_id=_get_correlation_id(request),
            source_ip=_get_source_ip(request),
            metadata={"group_id": group_id, "username": data.username, "role": role},
        )
        await db.commit()
        return {"group_id": group_id, "user_id": user.user_id, "username": user.username, "role": role}


@app.delete("/api/groups/{group_id}/members/{member_user_id}")
async def remove_group_member(request: Request, group_id: str, member_user_id: str):
    user_id = _get_user_id(request)
    async with async_session_factory() as db:
        group = await db.get(Group, group_id)
        if group is None:
            return JSONResponse(status_code=404, content=Error(error="Not found").model_dump())
        if await _group_member_role(db, group_id, user_id) != "admin":
            return JSONResponse(status_code=403, content=Error(error="Group admin only").model_dump())

        membership = await db.get(GroupMember, {"group_id": group_id, "user_id": member_user_id})
        if membership is None:
            return JSONResponse(status_code=404, content=Error(error="Not a member").model_dump())
        await db.delete(membership)
        await record_audit_event(
            db, "group.member_removed",
            actor_user_id=user_id,
            request_id=_get_request_id(request),
            correlation_id=_get_correlation_id(request),
            source_ip=_get_source_ip(request),
            metadata={"group_id": group_id, "member_user_id": member_user_id},
        )
        await db.commit()
        return Ok().model_dump()


@app.delete("/api/groups/{group_id}")
async def delete_group(request: Request, group_id: str):
    user_id = _get_user_id(request)
    async with async_session_factory() as db:
        group = await db.get(Group, group_id)
        if group is None:
            return JSONResponse(status_code=404, content=Error(error="Not found").model_dump())
        if await _group_member_role(db, group_id, user_id) != "admin":
            return JSONResponse(status_code=403, content=Error(error="Group admin only").model_dump())

        await db.delete(group)
        await record_audit_event(
            db, "group.deleted",
            actor_user_id=user_id,
            request_id=_get_request_id(request),
            correlation_id=_get_correlation_id(request),
            source_ip=_get_source_ip(request),
            metadata={"group_id": group_id, "name": group.name},
        )
        await db.commit()
        return Ok().model_dump()


# ─── Workspace sharing endpoints ─────────────────────────────────────

@app.post("/api/workspaces/{workspace_id}/shares", status_code=201)
async def share_workspace(request: Request, workspace_id: str):
    body = await request.json()
    data = ShareRequest(**body)
    user_id = _get_user_id(request)
    permission = data.permission if data.permission in ("view", "operate") else "view"
    async with async_session_factory() as db:
        ws = await db.get(Workspace, workspace_id)
        if ws is None:
            return JSONResponse(status_code=404, content=Error(error="Not found").model_dump())
        if not (_check_admin(request) or ws.user_id == user_id):
            return JSONResponse(status_code=403, content=Error(error="Workspace owner only").model_dump())

        group = await db.get(Group, data.group_id)
        if group is None:
            return JSONResponse(status_code=404, content=Error(error="Group not found").model_dump())
        existing = await db.get(WorkspaceShare, {"workspace_id": workspace_id, "group_id": data.group_id})
        if existing is not None:
            return JSONResponse(status_code=409, content=Error(error="Workspace already shared with this group").model_dump())

        db.add(WorkspaceShare(
            workspace_id=workspace_id,
            group_id=data.group_id,
            permission=permission,
            created_by=user_id,
        ))
        await record_audit_event(
            db, "workspace.shared",
            actor_user_id=user_id,
            workspace_id=workspace_id,
            request_id=_get_request_id(request),
            correlation_id=_get_correlation_id(request),
            source_ip=_get_source_ip(request),
            metadata={"group_id": data.group_id, "permission": permission},
        )
        await db.commit()
        return ShareOut(
            workspace_id=workspace_id,
            group_id=data.group_id,
            group_name=group.name,
            permission=permission,
            created_at=datetime.now(timezone.utc),
        ).model_dump()


@app.get("/api/workspaces/{workspace_id}/shares")
async def list_workspace_shares(request: Request, workspace_id: str):
    user_id = _get_user_id(request)
    async with async_session_factory() as db:
        ws = await db.get(Workspace, workspace_id)
        if ws is None:
            return JSONResponse(status_code=404, content=Error(error="Not found").model_dump())
        if not (_check_admin(request) or ws.user_id == user_id):
            return JSONResponse(status_code=403, content=Error(error="Workspace owner only").model_dump())

        result = await db.execute(
            select(WorkspaceShare, Group.name)
            .join(Group, Group.group_id == WorkspaceShare.group_id)
            .where(WorkspaceShare.workspace_id == workspace_id)
        )
        return [
            ShareOut(
                workspace_id=workspace_id,
                group_id=s.group_id,
                group_name=group_name,
                permission=s.permission,
                created_at=s.created_at,
            ).model_dump()
            for s, group_name in result.all()
        ]


@app.delete("/api/workspaces/{workspace_id}/shares/{group_id}")
async def unshare_workspace(request: Request, workspace_id: str, group_id: str):
    user_id = _get_user_id(request)
    async with async_session_factory() as db:
        ws = await db.get(Workspace, workspace_id)
        if ws is None:
            return JSONResponse(status_code=404, content=Error(error="Not found").model_dump())
        if not (_check_admin(request) or ws.user_id == user_id):
            return JSONResponse(status_code=403, content=Error(error="Workspace owner only").model_dump())

        share = await db.get(WorkspaceShare, {"workspace_id": workspace_id, "group_id": group_id})
        if share is None:
            return JSONResponse(status_code=404, content=Error(error="Not shared").model_dump())
        await db.delete(share)
        await record_audit_event(
            db, "workspace.unshared",
            actor_user_id=user_id,
            workspace_id=workspace_id,
            request_id=_get_request_id(request),
            correlation_id=_get_correlation_id(request),
            source_ip=_get_source_ip(request),
            metadata={"group_id": group_id},
        )
        await db.commit()
        return Ok().model_dump()

# ─── Network / egress control ────────────────────────────────────────

def _validate_allowlist(entries: list[str]) -> Optional[str]:
    """Return an error message for the first invalid entry, else None."""
    import ipaddress
    for entry in entries:
        entry = entry.strip()
        if not entry:
            return "allowlist entries must not be empty"
        try:
            ipaddress.ip_network(entry, strict=False)
            continue
        except ValueError:
            pass
        # Hostname: letters/digits/hyphens/dots, no scheme or port.
        if not re.match(r"^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)*$", entry):
            return f"invalid allowlist entry '{entry}' (expected CIDR or hostname)"
    return None


@app.get("/api/workspaces/{workspace_id}/network")
async def get_network_config(request: Request, workspace_id: str):
    """Read the workspace's egress mode and allowlist (view permission)."""
    async with async_session_factory() as db:
        ws = await _get_workspace_for_user(request, workspace_id, db)
        if ws is None:
            return JSONResponse(status_code=404, content=Error(error="Not found").model_dump())
        return NetworkConfigOut(
            workspace_id=workspace_id,
            mode=ws.network_mode,
            allowlist=ws.egress_allowlist or [],
        ).model_dump()


@app.patch("/api/workspaces/{workspace_id}/network")
async def update_network_config(request: Request, workspace_id: str):
    """Set the egress mode / allowlist (operate permission).

    The reconciler applies the change on its next pass — including for
    running workspaces, so a lockdown takes effect within ~30s.
    """
    body = await request.json()
    try:
        data = NetworkConfigUpdate(**body)
    except Exception:
        return JSONResponse(status_code=422, content=Error(error="Invalid request").model_dump())
    user_id = _get_user_id(request)
    async with async_session_factory() as db:
        ws = await _get_workspace_for_user(request, workspace_id, db)
        if ws is None:
            return JSONResponse(status_code=404, content=Error(error="Not found").model_dump())
        if not await _can_operate_workspace(request, ws, db):
            return JSONResponse(status_code=403, content=Error(error="Insufficient permission").model_dump())

        if data.mode is not None:
            ws.network_mode = data.mode
        if data.allowlist is not None:
            err = _validate_allowlist(data.allowlist)
            if err:
                return JSONResponse(status_code=400, content=Error(error=err).model_dump())
            ws.egress_allowlist = data.allowlist
        await record_audit_event(
            db, "workspace.network_changed",
            actor_user_id=user_id,
            workspace_id=workspace_id,
            request_id=_get_request_id(request),
            correlation_id=_get_correlation_id(request),
            source_ip=_get_source_ip(request),
            metadata={"mode": ws.network_mode, "allowlist": ws.egress_allowlist or []},
        )
        await db.commit()
        return NetworkConfigOut(
            workspace_id=workspace_id,
            mode=ws.network_mode,
            allowlist=ws.egress_allowlist or [],
        ).model_dump()


# ─── Workspace secrets ───────────────────────────────────────────────


@app.get("/api/workspaces/{workspace_id}/secrets")
async def list_workspace_secrets(request: Request, workspace_id: str):
    """List secret keys (names only). View permission is enough."""
    async with async_session_factory() as db:
        ws = await _get_workspace_for_user(request, workspace_id, db)
        if ws is None:
            return JSONResponse(status_code=404, content=Error(error="Not found").model_dump())
        result = await db.execute(
            select(WorkspaceSecret)
            .where(WorkspaceSecret.workspace_id == workspace_id)
            .order_by(WorkspaceSecret.key)
        )
        return [
            SecretOut(
                workspace_id=s.workspace_id,
                key=s.key,
                updated_at=s.updated_at,
            ).model_dump()
            for s in result.scalars().all()
        ]


@app.get("/api/workspaces/{workspace_id}/secrets/{key}")
async def get_workspace_secret(request: Request, workspace_id: str, key: str):
    """Read a secret's decrypted value (operate permission)."""
    async with async_session_factory() as db:
        ws = await _get_workspace_for_user(request, workspace_id, db)
        if ws is None:
            return JSONResponse(status_code=404, content=Error(error="Not found").model_dump())
        if not await _can_operate_workspace(request, ws, db):
            return JSONResponse(status_code=403, content=Error(error="Insufficient permission").model_dump())
        secret = await db.get(WorkspaceSecret, {"workspace_id": workspace_id, "key": key})
        if secret is None:
            return JSONResponse(status_code=404, content=Error(error="Not found").model_dump())
        try:
            value = decrypt_value(secret.value_encrypted)
        except SecretDecryptionError:
            return JSONResponse(
                status_code=500,
                content=Error(error="Failed to decrypt secret: encryption key rotated or lost").model_dump(),
            )
        return SecretValueOut(
            workspace_id=workspace_id,
            key=key,
            value=value,
            updated_at=secret.updated_at,
        ).model_dump()

@app.put("/api/workspaces/{workspace_id}/secrets/{key}")
async def upsert_workspace_secret(request: Request, workspace_id: str, key: str):
    """Create or update a secret (operate permission). Encrypted at rest."""
    if not re.match(SECRET_KEY_RE, key):
        return JSONResponse(status_code=400, content=Error(error="Invalid secret key: use [A-Za-z0-9_-]").model_dump())
    body = await request.json()
    data = SecretUpsert(**body)
    user_id = _get_user_id(request)
    async with async_session_factory() as db:
        ws = await _get_workspace_for_user(request, workspace_id, db)
        if ws is None:
            return JSONResponse(status_code=404, content=Error(error="Not found").model_dump())
        if not await _can_operate_workspace(request, ws, db):
            return JSONResponse(status_code=403, content=Error(error="Insufficient permission").model_dump())

        encrypted = encrypt_value(data.value)
        secret = await db.get(WorkspaceSecret, {"workspace_id": workspace_id, "key": key})
        if secret is None:
            secret = WorkspaceSecret(
                workspace_id=workspace_id,
                key=key,
                value_encrypted=encrypted,
                created_by=user_id,
            )
            db.add(secret)
            await record_audit_event(
                db, "workspace.secret_set",
                actor_user_id=user_id,
                workspace_id=workspace_id,
                request_id=_get_request_id(request),
                correlation_id=_get_correlation_id(request),
                source_ip=_get_source_ip(request),
                metadata={"key": key, "created": True},
            )
        else:
            secret.value_encrypted = encrypted
            await record_audit_event(
                db, "workspace.secret_set",
                actor_user_id=user_id,
                workspace_id=workspace_id,
                request_id=_get_request_id(request),
                correlation_id=_get_correlation_id(request),
                source_ip=_get_source_ip(request),
                metadata={"key": key, "created": False},
            )
        await db.commit()
        return SecretOut(
            workspace_id=workspace_id,
            key=key,
            updated_at=secret.updated_at,
        ).model_dump()


@app.delete("/api/workspaces/{workspace_id}/secrets/{key}")
async def delete_workspace_secret(request: Request, workspace_id: str, key: str):
    user_id = _get_user_id(request)
    async with async_session_factory() as db:
        ws = await _get_workspace_for_user(request, workspace_id, db)
        if ws is None:
            return JSONResponse(status_code=404, content=Error(error="Not found").model_dump())
        if not await _can_operate_workspace(request, ws, db):
            return JSONResponse(status_code=403, content=Error(error="Insufficient permission").model_dump())
        secret = await db.get(WorkspaceSecret, {"workspace_id": workspace_id, "key": key})
        if secret is None:
            return JSONResponse(status_code=404, content=Error(error="Not found").model_dump())
        await db.delete(secret)
        await record_audit_event(
            db, "workspace.secret_deleted",
            actor_user_id=user_id,
            workspace_id=workspace_id,
            request_id=_get_request_id(request),
            correlation_id=_get_correlation_id(request),
            source_ip=_get_source_ip(request),
            metadata={"key": key},
        )
        await db.commit()
        return Ok().model_dump()


# ─── MCP servers ─────────────────────────────────────────────────────

@app.get("/api/workspaces/{workspace_id}/mcp-servers")
async def list_mcp_servers(request: Request, workspace_id: str):
    async with async_session_factory() as db:
        ws = await _get_workspace_for_user(request, workspace_id, db)
        if ws is None:
            return JSONResponse(status_code=404, content=Error(error="Not found").model_dump())
        result = await db.execute(
            select(McpServer)
            .where(McpServer.workspace_id == workspace_id)
            .order_by(McpServer.created_at)
        )
        return [
            McpServerOut(
                server_id=s.server_id,
                workspace_id=s.workspace_id,
                name=s.name,
                port=s.port,
                enabled=s.enabled,
                created_at=s.created_at,
            ).model_dump()
            for s in result.scalars().all()
        ]


@app.post("/api/workspaces/{workspace_id}/mcp-servers", status_code=201)
async def register_mcp_server(request: Request, workspace_id: str):
    body = await request.json()
    data = McpServerCreate(**body)
    user_id = _get_user_id(request)
    async with async_session_factory() as db:
        ws = await _get_workspace_for_user(request, workspace_id, db)
        if ws is None:
            return JSONResponse(status_code=404, content=Error(error="Not found").model_dump())
        if not await _can_operate_workspace(request, ws, db):
            return JSONResponse(status_code=403, content=Error(error="Insufficient permission").model_dump())

        server_id = f"mcp-{uuid.uuid4()}"
        server = McpServer(
            server_id=server_id,
            workspace_id=workspace_id,
            name=data.name,
            port=data.port,
            enabled=data.enabled,
            created_by=user_id,
        )
        db.add(server)
        await record_audit_event(
            db, "mcp.server_registered",
            actor_user_id=user_id,
            workspace_id=workspace_id,
            request_id=_get_request_id(request),
            correlation_id=_get_correlation_id(request),
            source_ip=_get_source_ip(request),
            metadata={"server_id": server_id, "name": data.name, "port": data.port},
        )
        await db.commit()
        return McpServerOut(
            server_id=server_id,
            workspace_id=workspace_id,
            name=server.name,
            port=server.port,
            enabled=server.enabled,
            created_at=server.created_at,
        ).model_dump()


@app.patch("/api/workspaces/{workspace_id}/mcp-servers/{server_id}")
async def update_mcp_server(request: Request, workspace_id: str, server_id: str):
    body = await request.json()
    data = McpServerUpdate(**body)
    user_id = _get_user_id(request)
    async with async_session_factory() as db:
        ws = await _get_workspace_for_user(request, workspace_id, db)
        if ws is None:
            return JSONResponse(status_code=404, content=Error(error="Not found").model_dump())
        if not await _can_operate_workspace(request, ws, db):
            return JSONResponse(status_code=403, content=Error(error="Insufficient permission").model_dump())
        server = await db.get(McpServer, server_id)
        if server is None or server.workspace_id != workspace_id:
            return JSONResponse(status_code=404, content=Error(error="Not found").model_dump())
        if data.enabled is not None:
            server.enabled = data.enabled
        await db.commit()
        return McpServerOut(
            server_id=server.server_id,
            workspace_id=server.workspace_id,
            name=server.name,
            port=server.port,
            enabled=server.enabled,
            created_at=server.created_at,
        ).model_dump()


@app.delete("/api/workspaces/{workspace_id}/mcp-servers/{server_id}")
async def delete_mcp_server(request: Request, workspace_id: str, server_id: str):
    user_id = _get_user_id(request)
    async with async_session_factory() as db:
        ws = await _get_workspace_for_user(request, workspace_id, db)
        if ws is None:
            return JSONResponse(status_code=404, content=Error(error="Not found").model_dump())
        if not await _can_operate_workspace(request, ws, db):
            return JSONResponse(status_code=403, content=Error(error="Insufficient permission").model_dump())
        server = await db.get(McpServer, server_id)
        if server is None or server.workspace_id != workspace_id:
            return JSONResponse(status_code=404, content=Error(error="Not found").model_dump())
        await db.delete(server)
        await record_audit_event(
            db, "mcp.server_unregistered",
            actor_user_id=user_id,
            workspace_id=workspace_id,
            request_id=_get_request_id(request),
            correlation_id=_get_correlation_id(request),
            source_ip=_get_source_ip(request),
            metadata={"server_id": server_id},
        )
        await db.commit()
        return Ok().model_dump()


# ─── Usage & quotas ──────────────────────────────────────────────────

def _current_period_start() -> datetime:
    """First instant of the current UTC month (billing period)."""
    return datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


async def _monthly_totals(db: AsyncSession, user_id: str, period_start: datetime) -> dict[str, int]:
    """Sum usage by category for *user_id* since *period_start*."""
    result = await db.execute(
        select(UsageEvent.category, func.sum(UsageEvent.amount))
        .where(
            UsageEvent.user_id == user_id,
            UsageEvent.period_start == period_start,
        )
        .group_by(UsageEvent.category)
    )
    return {category: int(total or 0) for category, total in result.all()}


async def _get_quota(db: AsyncSession, user_id: str) -> Quota | None:
    return await db.get(Quota, user_id)


@app.post("/api/internal/usage", status_code=201)
async def ingest_usage(request: Request):
    """Append usage events reported by workspace agents (pod-originated).

    The pod authenticates with its per-workspace token (X-Workspace-Token);
    the caller's identity is the token's workspace OWNER. X-Service-User is
    ignored here — it was the cross-tenant impersonation vector and is only
    trusted from the gateway on other endpoints. Each event is recorded
    against the bound workspace regardless of any client-supplied
    ``workspace_id``.

    Token usage is enforced against the owner's monthly quota: events are
    always recorded (full accounting), but the response is 429 once the
    month's total exceeds ``max_monthly_tokens`` so the agent backs off.
    """
    agent_token = request.headers.get("X-Workspace-Token", "")
    if not agent_token:
        return JSONResponse(status_code=401, content=Error(error="Missing X-Workspace-Token").model_dump())

    body = await request.json()
    data = UsageIngestRequest(**body)
    async with async_session_factory() as db:
        ws_result = await db.execute(select(Workspace).where(Workspace.agent_token == agent_token))
        workspace = ws_result.scalar_one_or_none()
        if workspace is None:
            return JSONResponse(status_code=403, content=Error(error="Invalid workspace token").model_dump())

        user = await db.get(User, workspace.user_id)
        if user is None:
            return JSONResponse(status_code=403, content=Error(error="Invalid workspace token").model_dump())

        period = _current_period_start()
        token_delta = 0
        for ev in data.events:
            if ev.category not in USAGE_CATEGORIES:
                return JSONResponse(status_code=400, content=Error(error=f"Unknown category '{ev.category}'").model_dump())
            if ev.category == "tokens":
                token_delta += ev.amount
            db.add(UsageEvent(
                user_id=user.user_id,
                workspace_id=workspace.workspace_id,
                category=ev.category,
                metric=ev.metric,
                amount=ev.amount,
                unit=ev.unit,
                period_start=period,
            ))
        await db.commit()

        quota = await _get_quota(db, user.user_id)
        if quota and quota.max_monthly_tokens is not None and token_delta > 0:
            totals = await _monthly_totals(db, user.user_id, period)
            exceeded = totals.get("tokens", 0) > quota.max_monthly_tokens
            if exceeded:
                return JSONResponse(
                    status_code=429,
                    content=UsageIngestResponse(ok=True, quota_exceeded=True).model_dump(),
                )

        return UsageIngestResponse(ok=True).model_dump()


@app.get("/api/usage/summary")
async def usage_summary(request: Request):
    """Current-month usage totals and quota for the caller's own workspace."""
    user_id = _get_user_id(request)
    async with async_session_factory() as db:
        period = _current_period_start()
        totals = await _monthly_totals(db, user_id, period)
        quota = await _get_quota(db, user_id)
        max_tokens = quota.max_monthly_tokens if quota else None
        used = totals.get("tokens", 0)
        return UsageSummaryOut(
            user_id=user_id,
            period_start=period,
            totals=totals,
            max_monthly_tokens=max_tokens,
            tokens_remaining=max_tokens - used if max_tokens is not None else None,
            quota_exceeded=max_tokens is not None and used > max_tokens,
        ).model_dump()


@app.get("/api/admin/usage")
async def admin_list_usage(
    request: Request,
    user_id: Optional[str] = None,
    workspace_id: Optional[str] = None,
    category: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
):
    if not _check_admin(request):
        return JSONResponse(status_code=403, content=Error(error="Admin only").model_dump())
    async with async_session_factory() as db:
        query = select(UsageEvent)
        if user_id:
            query = query.where(UsageEvent.user_id == user_id)
        if workspace_id:
            query = query.where(UsageEvent.workspace_id == workspace_id)
        if category:
            query = query.where(UsageEvent.category == category)

        total_result = await db.execute(select(func.count()).select_from(query.subquery()))
        total = total_result.scalar() or 0
        query = query.order_by(desc(UsageEvent.recorded_at)).limit(limit).offset(offset)
        result = await db.execute(query)
        events = [
            {
                "id": e.id,
                "user_id": e.user_id,
                "workspace_id": e.workspace_id,
                "category": e.category,
                "metric": e.metric,
                "amount": e.amount,
                "unit": e.unit,
                "period_start": e.period_start,
                "recorded_at": e.recorded_at,
            }
            for e in result.scalars().all()
        ]
        return UsagePage(events=events, total=total, limit=limit, offset=offset).model_dump()


@app.get("/api/admin/quotas/{user_id}")
async def admin_get_quota(request: Request, user_id: str):
    if not _check_admin(request):
        return JSONResponse(status_code=403, content=Error(error="Admin only").model_dump())
    async with async_session_factory() as db:
        quota = await _get_quota(db, user_id)
        return QuotaOut(
            user_id=user_id,
            max_monthly_tokens=quota.max_monthly_tokens if quota else None,
            max_storage_gb=quota.max_storage_gb if quota else None,
        ).model_dump()


@app.put("/api/admin/quotas/{user_id}")
async def admin_set_quota(request: Request, user_id: str):
    if not _check_admin(request):
        return JSONResponse(status_code=403, content=Error(error="Admin only").model_dump())
    body = await request.json()
    data = QuotaUpdate(**body)
    async with async_session_factory() as db:
        quota = await _get_quota(db, user_id)
        if quota is None:
            quota = Quota(user_id=user_id)
            db.add(quota)
        if data.max_monthly_tokens is not None:
            quota.max_monthly_tokens = data.max_monthly_tokens
        if data.max_storage_gb is not None:
            quota.max_storage_gb = data.max_storage_gb
        await db.commit()
        return QuotaOut(
            user_id=user_id,
            max_monthly_tokens=quota.max_monthly_tokens,
            max_storage_gb=quota.max_storage_gb,
        ).model_dump()


# ─── Audit ingestion (internal) ──────────────────────────────────────

@app.post("/api/audit", status_code=201)
async def record_audit(request: Request):
    body = await request.json()
    ingest = body  # AuditEventIngest schema: {event_type, metadata?}

    event_type = ingest.get("event_type", "")
    metadata = ingest.get("metadata")

    # Derive actor from service identity context, not the request body
    actor_user_id = None
    # For gateway-triggered events, the gateway can include actor info
    # in X-Service-User header. Otherwise it's a system event.
    service_user = request.headers.get("X-Service-User", "")
    if service_user:
        actor_user_id = service_user

    async with async_session_factory() as db:
        await record_audit_event(
            db, event_type,
            actor_user_id=actor_user_id,
            request_id=_get_request_id(request),
            correlation_id=_get_correlation_id(request),
            source_ip=_get_source_ip(request),
            metadata=metadata,
        )
        await db.commit()

    return Ok().model_dump()


# ─── Health ───────────────────────────────────────────────────────────

@app.get("/api/health")
async def health():
    return HealthResponse(timestamp=datetime.now(timezone.utc)).model_dump()
