"""Alembic env — async-aware, autogenerate-friendly.

Reads ``DATABASE_URL`` at runtime so the same env.py works for tests
(testcontainers) and production (HA add-on / standalone Docker).

The ``include_object`` filter skips materialized views — they have
their DDL hand-managed in migrations because Alembic autogenerate
doesn't understand views.
"""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig
from typing import Any

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# Importing app.models registers every table on Base.metadata
from app.db import Base
from app import models  # noqa: F401  -- registers models on Base.metadata

target_metadata = Base.metadata

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

if (db_url := os.getenv("DATABASE_URL")) is not None:
    config.set_main_option("sqlalchemy.url", db_url)


def include_object(
    obj: Any,
    name: str,  # noqa: ARG001
    type_: str,
    reflected: bool,  # noqa: ARG001
    compare_to: Any,  # noqa: ARG001
) -> bool:
    """Skip materialized-view-mapped tables from autogenerate diffs."""
    if type_ == "table" and obj.info.get("is_view"):
        return False
    return True


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_object=include_object,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=include_object,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
