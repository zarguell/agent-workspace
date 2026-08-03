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


# ─── Groups & workspace sharing ─────────────────────────────────────

class GroupOut(BaseModel):
    group_id: str
    name: str
    created_at: datetime
    role: Optional[str] = None  # caller's role in the group, when listed for a user


class GroupDetail(BaseModel):
    group_id: str
    name: str
    created_at: datetime
    members: list[dict]  # [{user_id, username, display_name, role, joined_at}]


class CreateGroupRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)


class AddMemberRequest(BaseModel):
    username: str = Field(min_length=1, max_length=255)
    role: str = "member"  # admin | member


class ShareRequest(BaseModel):
    group_id: str = Field(min_length=1, max_length=255)
    permission: str = "view"  # view | operate


class ShareOut(BaseModel):
    workspace_id: str
    group_id: str
    group_name: str
    permission: str
    created_at: datetime


# ─── Usage & quotas ──────────────────────────────────────────────────

USAGE_CATEGORIES = ("tokens", "compute", "storage")


class UsageEventIn(BaseModel):
    category: str = Field(min_length=1, max_length=20)
    metric: str = Field(min_length=1, max_length=100)
    amount: int = Field(ge=0)
    unit: str = Field(min_length=1, max_length=20)
    workspace_id: Optional[str] = Field(default=None, max_length=255)


class UsageIngestRequest(BaseModel):
    events: list[UsageEventIn] = Field(min_length=1, max_length=1000)


class UsageIngestResponse(BaseModel):
    ok: bool = True
    quota_exceeded: bool = False


class UsageSummaryOut(BaseModel):
    user_id: str
    period_start: datetime
    totals: dict[str, int]  # category -> amount for the current month
    max_monthly_tokens: Optional[int] = None
    tokens_remaining: Optional[int] = None
    quota_exceeded: bool = False


class QuotaOut(BaseModel):
    user_id: str
    max_monthly_tokens: Optional[int] = None
    max_storage_gb: Optional[int] = None


class QuotaUpdate(BaseModel):
    max_monthly_tokens: Optional[int] = Field(default=None, ge=0)
    max_storage_gb: Optional[int] = Field(default=None, ge=0)


class UsagePage(BaseModel):
    events: list[dict]
    total: int
    limit: int
    offset: int


# ─── MCP servers ─────────────────────────────────────────────────────

class McpServerOut(BaseModel):
    server_id: str
    workspace_id: str
    name: str
    port: int = Field(ge=1, le=65535)
    enabled: bool = True
    created_at: datetime


class McpServerCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    port: int = Field(ge=1, le=65535)
    enabled: bool = True


class McpServerUpdate(BaseModel):
    enabled: Optional[bool] = None


class McpTargetOut(BaseModel):
    workspace_id: str
    server_id: str
    name: str
    port: int
    enabled: bool


# ─── Workspace secrets ───────────────────────────────────────────────

SECRET_KEY_RE = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$"


class SecretOut(BaseModel):
    workspace_id: str
    key: str
    updated_at: datetime


class SecretValueOut(SecretOut):
    value: str


class SecretUpsert(BaseModel):
    value: str = Field(max_length=8000)


# ─── Network / egress control ───────────────────────────────────────

NETWORK_MODES = ("open", "offline", "allowlist")


class NetworkConfigOut(BaseModel):
    workspace_id: str
    mode: str
    allowlist: list[str] = []


class NetworkConfigUpdate(BaseModel):
    mode: Optional[str] = Field(default=None, pattern="^(open|offline|allowlist)$")
    allowlist: Optional[list[str]] = Field(default=None, max_length=50)
