"""Password hashing, session creation, and session validation."""

import os
import uuid
from datetime import datetime, timedelta, timezone

import bcrypt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import Session, User

SESSION_COOKIE_NAME = "session"
SESSION_TTL_HOURS = 24

COOKIE_DOMAIN = os.environ.get("COOKIE_DOMAIN", "")
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "false").lower() == "true"


def hash_password(password: str) -> str:
    """Return bcrypt hash of password."""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """Verify a password against its bcrypt hash."""
    return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))


async def authenticate_user(db: AsyncSession, username: str, password: str) -> User | None:
    """Validate username/password. Returns User or None."""
    result = await db.execute(select(User).where(User.username == username))
    user = result.scalar_one_or_none()
    if user is None:
        return None
    if user.disabled_at is not None:
        return None
    if not verify_password(password, user.password_hash):
        return None
    return user


async def create_session(db: AsyncSession, user: User) -> Session:
    """Create a session record in DB and return it."""
    session_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(hours=SESSION_TTL_HOURS)
    session = Session(
        session_id=session_id,
        user_id=user.user_id,
        created_at=now,
        expires_at=expires_at,
    )
    db.add(session)
    await db.flush()
    return session


def make_session_cookie(session_id: str) -> dict:
    """Return Set-Cookie params for the session."""
    return {
        "key": SESSION_COOKIE_NAME,
        "value": session_id,
        "httponly": True,
        "samesite": "strict",
        "secure": COOKIE_SECURE,
        "domain": COOKIE_DOMAIN or None,
        "max_age": SESSION_TTL_HOURS * 3600,
        "path": "/",
    }


def delete_session_cookie() -> dict:
    """Return Set-Cookie params that clear the session cookie."""
    return {
        "key": SESSION_COOKIE_NAME,
        "value": "",
        "httponly": True,
        "samesite": "strict",
        "secure": COOKIE_SECURE,
        "domain": COOKIE_DOMAIN or None,
        "max_age": 0,
        "path": "/",
    }


async def validate_session(db: AsyncSession, session_id: str) -> User | None:
    """Look up session, check expiry, return User or None."""
    if not session_id:
        return None
    result = await db.execute(
        select(Session).where(Session.session_id == session_id)
    )
    session = result.scalar_one_or_none()
    if session is None:
        return None
    now = datetime.now(timezone.utc)
    if session.expires_at < now:
        await db.delete(session)
        await db.flush()
        return None
    user_result = await db.execute(
        select(User).where(User.user_id == session.user_id)
    )
    user = user_result.scalar_one_or_none()
    if user is None or user.disabled_at is not None:
        return None
    return user
