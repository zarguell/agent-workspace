"""Audit event helper — structured logging to Postgres + stdout."""

import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from models import AuditEvent

logger = logging.getLogger("control-plane.audit")


async def record_audit_event(
    db: AsyncSession,
    event_type: str,
    actor_user_id: Optional[str] = None,
    workspace_id: Optional[str] = None,
    request_id: Optional[str] = None,
    correlation_id: Optional[str] = None,
    source_ip: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> AuditEvent:
    """Create an audit event, write to DB, and log to stdout.

    No passwords, tokens, prompt contents, file contents, or secrets
    are written to audit logs.
    """
    now = datetime.now(timezone.utc)
    event = AuditEvent(
        timestamp=now,
        event_type=event_type,
        actor_user_id=actor_user_id,
        workspace_id=workspace_id,
        request_id=request_id,
        correlation_id=correlation_id,
        source_ip=source_ip,
        metadata_=metadata,
    )
    db.add(event)
    await db.flush()

    # Also log to stdout
    log_entry = {
        "timestamp": now.isoformat(),
        "event_type": event_type,
        "actor_user_id": actor_user_id,
        "workspace_id": workspace_id,
        "request_id": request_id,
        "correlation_id": correlation_id,
        "source_ip": source_ip,
        "metadata": metadata,
    }
    logger.info("AUDIT %s", json.dumps(log_entry, default=str))

    return event
