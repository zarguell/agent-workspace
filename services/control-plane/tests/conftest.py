"""Test fixtures for the control-plane suite.

Postgres strategy (per plan): TEST_DATABASE_URL env wins; otherwise a
testcontainers postgres:16-alpine (matches the Helm chart). If neither is
available the suite skips explicitly — never a silent pass.

Import rule: control-plane modules read DATABASE_URL / SERVICE_AUTH_TOKEN
at import time, so `main` / `database` / `reconciler` / `models` are
imported lazily inside fixtures, never at test-module top level.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("SERVICE_AUTH_TOKEN", "test-service-token")

import httpx
import pytest
import pytest_asyncio


# ─── DB fixture ─────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def postgres():
    url = os.environ.get("TEST_DATABASE_URL")
    container = None
    if not url:
        try:
            from testcontainers.community.postgres import PostgresContainer
            container = PostgresContainer("postgres:16-alpine")
            container.start()
            url = container.get_connection_url().replace("+psycopg2", "+asyncpg")
        except Exception as e:  # noqa: BLE001 - skip on any startup failure
            pytest.skip(f"control-plane tests need TEST_DATABASE_URL or a running Docker daemon ({e})")

    old = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    try:
        yield url
    finally:
        if old is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = old
        if container:
            container.stop()
@pytest_asyncio.fixture
async def db_ready(postgres):
    """Run the same init_db() the production lifespan runs.

    Import the models first so Base.metadata is populated before
    create_all — otherwise the metadata is empty and no tables appear.
    """
    from models import AuditEvent, Session, User, Workspace  # noqa: F401 - register tables
    from database import init_db, engine
    await init_db()
    # Mirror the production migration state: the canvas-key columns were
    # added via ALTER TABLE ADD COLUMN (nullable), so legacy rows carry NULL
    # and the reconciler backfills them. create_all builds them NOT NULL,
    # which would make the backfill path untestable.
    from sqlalchemy import text
    async with engine.begin() as conn:
        await conn.execute(text(
            "ALTER TABLE workspaces ALTER COLUMN canvas_api_key DROP NOT NULL, "
            "ALTER COLUMN canvas_secret_key DROP NOT NULL"
        ))
@pytest_asyncio.fixture
async def db(db_ready):
    """Async session bound to the test database."""
    from database import async_session_factory
    async with async_session_factory() as session:
        yield session


# ─── App + client fixtures ──────────────────────────────────────────────

@pytest_asyncio.fixture
async def app(db_ready):
    """The real control-plane FastAPI app (production lifespan NOT run)."""
    from unittest.mock import AsyncMock
    from main import app as real_app
    from reconciler import reconciler

    # The routing endpoint pokes K8s + the pod network; stub the singleton
    # so API tests are deterministic and offline.
    reconciler._get_cluster_ip = AsyncMock(return_value=None)
    reconciler._check_pod_ready = AsyncMock(return_value=False)
    reconciler._check_pod_ready_host = AsyncMock(return_value=False)
    return real_app


@pytest_asyncio.fixture
async def client(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture(autouse=True)
async def _clear_idempotency():
    """The idempotency store is a process-global singleton; isolate tests."""
    from idempotency import idempotency_store
    idempotency_store._store.clear()
    yield
    idempotency_store._store.clear()


@pytest.fixture
def service_token():
    return os.environ["SERVICE_AUTH_TOKEN"]


# ─── Seeding helpers ────────────────────────────────────────────────────

async def seed_user(username, password="pw", is_admin=False, create_workspace=True):
    """Insert a user (and optionally their workspace) idempotently."""
    from sqlalchemy import select
    from auth import hash_password
    from database import async_session_factory
    from models import User, Workspace

    async with async_session_factory() as db:
        result = await db.execute(select(User).where(User.username == username))
        user = result.scalar_one_or_none()
        if user is None:
            user = User(
                username=username,
                password_hash=hash_password(password),
                display_name=username.capitalize(),
                is_admin=is_admin,
            )
            db.add(user)
            await db.flush()
        if create_workspace:
            ws = await db.get(Workspace, f"ws-{username}")
            if ws is None:
                db.add(Workspace(
                    workspace_id=f"ws-{username}",
                    user_id=user.user_id,
                    state="requested",
                    image="",
                ))
        await db.commit()
        return user
