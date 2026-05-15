"""Async SQLAlchemy engine + session factory + declarative ``Base``.

The HMAC keys for the audit-chain trigger live in Postgres session GUCs
(``audit.hmac_key_<id>``), set on every session checkout by
``get_session()``. Without those GUCs the trigger raises and audit
INSERTs fail, which is intentional — silent loss of audit signing would
be worse than a hard failure.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from sqlalchemy import MetaData, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings

if TYPE_CHECKING:
    pass


# Stable constraint/index names so Alembic autogenerate produces clean diffs
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative base for all SQLAlchemy 2.0 models in this app."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    """Lazy singleton engine. Reset via ``dispose_engine()`` (e.g. tests)."""
    global _engine  # noqa: PLW0603 — module-level lazy singleton is the intent
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(
            settings.database_url,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=10,
            future=True,
        )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory  # noqa: PLW0603 — module-level lazy singleton
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            get_engine(), expire_on_commit=False, class_=AsyncSession
        )
    return _session_factory


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency. Sets HMAC key GUCs before yielding."""
    factory = get_session_factory()
    settings = get_settings()
    async with factory() as session:
        for key_id, key_hex in settings.hmac_keys.items():
            # key_id is forced to int — never user-controlled in SQL
            await session.execute(
                text(f"SELECT set_config('audit.hmac_key_{int(key_id)}', :v, false)"),
                {"v": key_hex},
            )
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    """Tear down the cached engine (used by tests + lifespan shutdown)."""
    global _engine, _session_factory  # noqa: PLW0603 — singleton lifecycle
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None
