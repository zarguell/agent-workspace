"""Async SQLAlchemy engine and session factory for Postgres."""

import os

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+asyncpg://agent:agent@localhost:5432/agentplatform",
)

engine = create_async_engine(DATABASE_URL, pool_size=20, max_overflow=10, pool_pre_ping=True)

async_session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_session() -> AsyncSession:
    """Yields an async session. Use as a FastAPI dependency or context manager."""
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def init_db():
    """Create all tables and apply lightweight migrations.

    Safe to call on startup — CREATE TABLE IF NOT EXISTS for fresh installs,
    plus idempotent ALTER TABLE for columns added after the first deploy.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Columns added in later versions — no-op on fresh installs. The
        # canvas-key columns were widened to VARCHAR(255) to fit Fernet
        # tokens (~120 chars); ALTER COLUMN TYPE is idempotent.
        for stmt in [
            "ALTER TABLE workspaces ALTER COLUMN canvas_api_key TYPE VARCHAR(255)",
            "ALTER TABLE workspaces ALTER COLUMN canvas_secret_key TYPE VARCHAR(255)",
            "ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS network_mode VARCHAR(20) DEFAULT 'open'",
            "ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS egress_allowlist JSONB DEFAULT '[]'::jsonb",
            "ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS agent_token VARCHAR(64)",
            "ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS preserve_pvc BOOLEAN NOT NULL DEFAULT FALSE",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS oidc_sub VARCHAR(255)",
        ]:
            await conn.execute(text(stmt))
        await conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_users_oidc_sub ON users (oidc_sub)"
        ))
