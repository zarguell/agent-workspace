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

# Loud startup warning: Secure off means session cookies traverse the
# network in cleartext. Dev stacks (plain HTTP on localhost/agents.local.test)
# need it off; production behind TLS MUST enable it.
if not COOKIE_SECURE:
    import logging
    logging.getLogger(__name__).warning(
        "COOKIE_SECURE is disabled: session cookies will be sent without the "
        "Secure flag. Enable COOKIE_SECURE=true when serving over TLS."
    )


def hash_password(password: str) -> str:
    """Return bcrypt hash of password."""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """Verify a password against its bcrypt hash."""
    return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))


# A valid bcrypt hash (cost 12, matching hash_password) of a throwaway
# password. Used as a timing sink: when the username does not exist we still
# run a full bcrypt check so the response time does not reveal existence.
_DUMMY_PASSWORD_HASH = "$2b$12$FJU37WIw3OHp9vhbfj8Xoe/1Jh.h1owjsKnwc/0sT3pdhyrKXuOOW"

async def authenticate_user(db: AsyncSession, username: str, password: str) -> User | None:
    """Validate username/password. Returns User or None."""
    result = await db.execute(select(User).where(User.username == username))
    user = result.scalar_one_or_none()
    if user is None:
        # Equalize response time with the wrong-password path.
        verify_password(password, _DUMMY_PASSWORD_HASH)
        return None
    if user.disabled_at is not None:
        return None
    try:
        ok = verify_password(password, user.password_hash)
    except ValueError:
        # Unparseable hash: SSO accounts store password_hash="!" so a password
        # attempt against them must be a uniform 401, never a 500.
        ok = False
    if not ok:
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
