"""Pytest fixtures.

Integration tests run against a real Postgres 16 inside a testcontainers
container. The container is shared across the whole session; each test
gets its own ``AsyncSession`` and rolls back on teardown so tests are
isolated even though the schema persists.

Unit tests (``backend/tests/unit/``) do not request ``session`` and
therefore do NOT trigger the container, so they work without Docker.

If Docker isn't available, integration tests are skipped with a clear
message rather than failing opaquely.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import AsyncIterator, Generator
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

# Test HMAC key — fresh 32 random bytes per session, hex-encoded.
TEST_HMAC_KEY = secrets.token_hex(32)


def _docker_available() -> bool:
    """Return True if a Docker daemon is reachable."""
    try:
        import docker  # noqa: PLC0415
    except ImportError:
        return False
    try:
        client = docker.from_env()
        client.ping()
    except Exception:
        return False
    else:
        return True


@pytest.fixture(scope="session")
def _postgres_container():  # type: ignore[no-untyped-def]
    """Spin up Postgres 16 once per session. Triggered only by ``session`` fixture."""
    if not _docker_available():
        pytest.skip(
            "Docker not available; integration tests skipped",
            allow_module_level=False,
        )

    from testcontainers.postgres import PostgresContainer  # noqa: PLC0415

    pg = PostgresContainer("postgres:16-alpine")
    with pg as container:
        yield container


@pytest.fixture(scope="session")
def database_url(_postgres_container) -> str:  # type: ignore[no-untyped-def]
    """Async-driver SQLAlchemy URL for the test container."""
    raw = _postgres_container.get_connection_url()
    if "+psycopg2" in raw:
        return raw.replace("+psycopg2", "+asyncpg")
    if "+psycopg" in raw:
        return raw.replace("+psycopg", "+asyncpg")
    return raw.replace("postgresql://", "postgresql+asyncpg://")


@pytest.fixture(scope="session")
def _set_env(database_url: str) -> Generator[None, None, None]:
    """Wire env vars BEFORE any session-using test imports app.config."""
    prior = {
        k: os.environ.get(k)
        for k in ("DATABASE_URL", "HMAC_KEY_1", "HMAC_KEY_ID_CURRENT")
    }
    os.environ["DATABASE_URL"] = database_url
    os.environ["HMAC_KEY_1"] = TEST_HMAC_KEY
    os.environ["HMAC_KEY_ID_CURRENT"] = "1"

    from app.config import get_settings  # noqa: PLC0415

    get_settings.cache_clear()
    yield
    for k, v in prior.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    get_settings.cache_clear()


@pytest.fixture(scope="session")
def _migrated(_set_env, database_url: str) -> Generator[None, None, None]:  # type: ignore[no-untyped-def]
    """Run ``alembic upgrade head`` once per test session."""
    from alembic import command  # noqa: PLC0415
    from alembic.config import Config as AlembicConfig  # noqa: PLC0415

    backend_dir = Path(__file__).resolve().parent.parent
    alembic_ini = backend_dir / "alembic.ini"
    cfg = AlembicConfig(str(alembic_ini))
    cfg.set_main_option(
        "script_location", str(backend_dir / "alembic")
    )
    command.upgrade(cfg, "head")
    return


@pytest_asyncio.fixture
async def async_engine(_migrated, database_url: str) -> AsyncIterator[AsyncEngine]:  # type: ignore[no-untyped-def]
    engine = create_async_engine(
        database_url,
        future=True,
        pool_pre_ping=True,
        pool_size=2,
        max_overflow=4,
    )
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def session(async_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """Per-test AsyncSession with HMAC GUC pre-set."""
    factory = async_sessionmaker(
        async_engine, expire_on_commit=False, class_=AsyncSession
    )
    async with factory() as s:
        await s.execute(
            text("SELECT set_config('audit.hmac_key_1', :v, false)"),
            {"v": TEST_HMAC_KEY},
        )
        try:
            yield s
        finally:
            await s.rollback()
