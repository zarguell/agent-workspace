"""Pydantic schemas for API request/response, matching OpenAPI spec."""

from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from pydantic import BaseModel, Field


# ─── Primitive ───────────────────────────────────────────────────────

class Ok(BaseModel):
    ok: bool = True
    correlation_id: Optional[str] = None


class Error(BaseModel):
    error: str
    detail: Optional[str] = None
    correlation_id: Optional[str] = None


# ─── Auth / Session ──────────────────────────────────────────────────

class UserOut(BaseModel):
    user_id: str
    username: str
    display_name: str
    is_admin: bool
    created_at: datetime
    disabled_at: Optional[datetime] = None


class SessionOut(BaseModel):
    user_id: str
    username: str
    display_name: str
    is_admin: bool
    session_id: Optional[str] = None
    created_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    user_id: str
    username: str
    display_name: str
    is_admin: bool
    redirect: str = "/ui/workspaces"


# ─── Workspace ───────────────────────────────────────────────────────

class WorkspaceState(str):
    pass


WORKSPACE_STATES = [
    "requested", "starting", "running", "idle_pending",
    "hibernating", "hibernated", "failed", "deleting", "deleted",
]


class Exposure(BaseModel):
    id: Optional[str] = None
    port: int
    name: str
    protocol: str = "http"
    health_path: str = "/"
    status: str = "registered"
    registered_at: Optional[datetime] = None


class WorkspaceOut(BaseModel):
    model_config = {"from_attributes": True}

    workspace_id: str
    user_id: str
    username: Optional[str] = None
    state: str
    image: Optional[str] = None
    created_at: datetime
    started_at: Optional[datetime] = None
    last_activity_at: Optional[datetime] = None
    idle_timeout_minutes: int = 15
    exposures: list[Exposure] = []


class WorkspaceStatus(BaseModel):
    workspace_id: str
    state: str
    services: Optional[dict[str, str]] = None
    exposures: list[Exposure] = []
    last_activity_at: Optional[datetime] = None
    error_message: Optional[str] = None


class WorkspaceRoutingStatus(BaseModel):
    workspace_id: str
    state: str
    cluster_ip: Optional[str] = None
    ports: dict[str, int] = {"paseo": 6767, "code_server": 8080, "agent": 9000}
    agent_ready: bool = False
    exposures: list[Exposure] = []


class Operation(BaseModel):
    workspace_id: str
    state: str
    operation_id: str
    requested_at: datetime
    correlation_id: Optional[str] = None


# ─── Audit ───────────────────────────────────────────────────────────

class AuditEventIngest(BaseModel):
    event_type: str
    metadata: Optional[dict[str, Any]] = None


class AuditEventOut(BaseModel):
    timestamp: datetime
    event_type: str
    actor_user_id: Optional[str] = None
    workspace_id: Optional[str] = None
    request_id: Optional[str] = None
    correlation_id: Optional[str] = None
    source_ip: Optional[str] = None
    metadata: Optional[dict[str, Any]] = None


class AuditPage(BaseModel):
    events: list[AuditEventOut]
    total: int
    limit: int
    offset: int


# ─── Admin ───────────────────────────────────────────────────────────

class WorkspaceDeleteResponse(BaseModel):
    workspace_id: str
    state: str
    operation_id: str


# ─── Health ──────────────────────────────────────────────────────────

class HealthResponse(BaseModel):
    status: str = "ok"
    service: str = "control-plane"
    timestamp: datetime
