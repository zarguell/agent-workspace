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
