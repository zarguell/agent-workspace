"""SQLAlchemy ORM models for the control-plane database."""

import secrets
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import relationship

from database import Base


def _utcnow():
    return datetime.now(timezone.utc)


def _new_uuid():
    return str(uuid.uuid4())


def _new_canvas_key():
    """Per-workspace secret for the workspace's Agent Canvas instance."""
    return secrets.token_hex(32)


class User(Base):
    __tablename__ = "users"

    user_id = Column(UUID(as_uuid=False), primary_key=True, default=_new_uuid)
    username = Column(String(255), unique=True, nullable=False, index=True)
    password_hash = Column(Text, nullable=False)
    display_name = Column(String(255), nullable=False)
    is_admin = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
    disabled_at = Column(DateTime(timezone=True), nullable=True)

    sessions = relationship("Session", back_populates="user", cascade="all, delete-orphan")
    workspace = relationship("Workspace", back_populates="user", uselist=False, cascade="all, delete-orphan")


class Session(Base):
    __tablename__ = "sessions"

    session_id = Column(UUID(as_uuid=False), primary_key=True, default=_new_uuid)
    user_id = Column(UUID(as_uuid=False), ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)

    user = relationship("User", back_populates="sessions")


class Workspace(Base):
    __tablename__ = "workspaces"

    workspace_id = Column(String(255), primary_key=True)  # e.g. ws-<opaque>
    user_id = Column(
        UUID(as_uuid=False),
        ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    state = Column(String(50), nullable=False, default="requested")
    image = Column(String(500), nullable=False)
    idle_timeout_minutes = Column(Integer, default=15, nullable=False)
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
    started_at = Column(DateTime(timezone=True), nullable=True)
    last_activity_at = Column(DateTime(timezone=True), nullable=True)

    # Per-workspace secrets for the workspace's Agent Canvas instance.
    # Persisted in the DB so they survive pod hibernate/recreate; the Canvas
    # state dir is on the PVC but keys default to the container layer.
    canvas_api_key = Column(String(64), nullable=False, default=_new_canvas_key)
    canvas_secret_key = Column(String(64), nullable=False, default=_new_canvas_key)

    # Egress control: open (default), offline (no egress), or allowlist
    # (DNS + platform services + explicit hosts/CIDRs only). Enforced by the
    # reconciler's NetworkPolicy.
    network_mode = Column(String(20), nullable=False, default="open")
    egress_allowlist = Column(JSONB, nullable=False, default=list)

    __table_args__ = (
        CheckConstraint(
            state.in_([
                "requested", "starting", "running", "idle_pending",
                "hibernating", "hibernated", "failed", "deleting", "deleted",
            ]),
            name="ck_workspace_state",
        ),
    )

    user = relationship("User", back_populates="workspace")


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime(timezone=True), default=_utcnow, nullable=False, index=True)
    event_type = Column(String(100), nullable=False, index=True)
    actor_user_id = Column(UUID(as_uuid=False), nullable=True, index=True)
    workspace_id = Column(String(255), nullable=True, index=True)
    request_id = Column(String(255), nullable=True)
    correlation_id = Column(String(255), nullable=True)
    source_ip = Column(String(45), nullable=True)
    metadata_ = Column("metadata", JSONB, nullable=True)


class Group(Base):
    """A named collection of users; workspaces can be shared with a group."""

    __tablename__ = "groups"

    group_id = Column(String(255), primary_key=True)  # grp-<uuid>
    name = Column(String(255), unique=True, nullable=False, index=True)
    created_by = Column(
        UUID(as_uuid=False),
        ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False,
    )
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)

    members = relationship(
        "GroupMember",
        back_populates="group",
        cascade="all, delete-orphan",
    )


class GroupMember(Base):
    """Membership of a user in a group, with a role."""

    __tablename__ = "group_members"

    group_id = Column(
        String(255),
        ForeignKey("groups.group_id", ondelete="CASCADE"),
        primary_key=True,
    )
    user_id = Column(
        UUID(as_uuid=False),
        ForeignKey("users.user_id", ondelete="CASCADE"),
        primary_key=True,
    )
    role = Column(String(20), nullable=False, default="member")  # admin | member
    joined_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)

    group = relationship("Group", back_populates="members")


class WorkspaceShare(Base):
    """Grant of a permission on a workspace to a group."""

    __tablename__ = "workspace_shares"

    workspace_id = Column(
        String(255),
        ForeignKey("workspaces.workspace_id", ondelete="CASCADE"),
        primary_key=True,
    )
    group_id = Column(
        String(255),
        ForeignKey("groups.group_id", ondelete="CASCADE"),
        primary_key=True,
    )
    permission = Column(String(20), nullable=False, default="view")  # view | operate
    created_by = Column(
        UUID(as_uuid=False),
        ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False,
    )
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)


class Quota(Base):
    """Per-user resource and budget limits. NULL means unlimited."""

    __tablename__ = "quotas"

    user_id = Column(
        UUID(as_uuid=False),
        ForeignKey("users.user_id", ondelete="CASCADE"),
        primary_key=True,
    )
    max_monthly_tokens = Column(BigInteger, nullable=True)
    max_storage_gb = Column(Integer, nullable=True)
    updated_at = Column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)


class UsageEvent(Base):
    """Append-only usage ledger row, reported by workspace agents."""

    __tablename__ = "usage_events"
    __table_args__ = (
        Index("ix_usage_user_period", "user_id", "period_start"),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(UUID(as_uuid=False), nullable=False, index=True)
    workspace_id = Column(String(255), nullable=False, index=True)
    category = Column(String(20), nullable=False, index=True)  # tokens | compute | storage
    metric = Column(String(100), nullable=False)
    amount = Column(BigInteger, nullable=False)
    unit = Column(String(20), nullable=False)
    # First day of the UTC month the usage belongs to (billing period).
    period_start = Column(DateTime(timezone=True), nullable=False, index=True)
    recorded_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)


class McpServer(Base):
    """A registered MCP (Model Context Protocol) server for a workspace.

    MCP servers run in (or alongside) the workspace pod; the gateway proxies
    /mcp/{server_id} to http://{workspace-cluster-ip}:{port} after
    authenticating the session and authorizing the workspace access.
    """

    __tablename__ = "mcp_servers"

    server_id = Column(String(255), primary_key=True)  # mcp-<uuid>
    workspace_id = Column(
        String(255),
        ForeignKey("workspaces.workspace_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name = Column(String(255), nullable=False)
    port = Column(Integer, nullable=False)
    enabled = Column(Boolean, default=True, nullable=False)
    created_by = Column(
        UUID(as_uuid=False),
        ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False,
    )
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)


class WorkspaceSecret(Base):
    """Per-workspace secret, encrypted at rest (Fernet, SECRETS_MASTER_KEY).

    Values are injected into workspace pods as WS_SECRET_<KEY> env vars by
    the reconciler. Only the encrypted blob is stored in Postgres.
    """

    __tablename__ = "workspace_secrets"
    __table_args__ = (UniqueConstraint("workspace_id", "key"),)

    workspace_id = Column(
        String(255),
        ForeignKey("workspaces.workspace_id", ondelete="CASCADE"),
        primary_key=True,
    )
    key = Column(String(255), primary_key=True)
    value_encrypted = Column(Text, nullable=False)
    created_by = Column(
        UUID(as_uuid=False),
        ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False,
    )
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)
