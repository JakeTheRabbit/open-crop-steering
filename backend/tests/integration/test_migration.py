"""Verify the baseline migration produces the expected schema."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

EXPECTED_TABLES = {
    "users",
    "roles",
    "user_roles",
    "recipe_revision",
    "recipe_revision_param",
    "runtime_adjustment",
    "command_batch",
    "command_queue",
    "audit_event",
    "daily_seal",
    "event_log",
    "pending_approval",
    "telegram_user_map",
    "cumulative_delta",
    "sensor_snapshot",
    "llm_call_log",
    "room_runtime",  # added by migration 0002
    "audit_chain_head",  # added by migration 0003
    "alembic_version",  # added by alembic itself
}

EXPECTED_ENUMS = {
    "role_name",
    "recipe_status",
    "adjustment_source",
    "adjustment_mode",
    "command_status",
    "audit_event_type",
    "event_severity",
    "pending_status",
    "llm_call_outcome",
}


async def test_all_tables_present(session: AsyncSession) -> None:
    result = await session.execute(
        text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    )
    actual = {row[0] for row in result.all()}
    missing = EXPECTED_TABLES - actual
    assert not missing, f"missing tables: {sorted(missing)}"


async def test_all_enum_types_present(session: AsyncSession) -> None:
    result = await session.execute(
        text("SELECT typname FROM pg_type WHERE typtype = 'e'")
    )
    actual = {row[0] for row in result.all()}
    missing = EXPECTED_ENUMS - actual
    assert not missing, f"missing enum types: {sorted(missing)}"


async def test_pgcrypto_extension_loaded(session: AsyncSession) -> None:
    result = await session.execute(
        text("SELECT 1 FROM pg_extension WHERE extname = 'pgcrypto'")
    )
    assert result.scalar() == 1


async def test_effective_target_materialized_view_exists(
    session: AsyncSession,
) -> None:
    result = await session.execute(
        text("SELECT 1 FROM pg_matviews WHERE matviewname = 'effective_target'")
    )
    assert result.scalar() == 1


async def test_audit_event_chain_triggers_present(session: AsyncSession) -> None:
    result = await session.execute(
        text(
            "SELECT trigger_name FROM information_schema.triggers "
            "WHERE event_object_table = 'audit_event'"
        )
    )
    triggers = {row[0] for row in result.all()}
    assert "trg_audit_event_hmac_chain" in triggers
    assert "trg_audit_event_no_update" in triggers
    assert "trg_audit_event_no_delete" in triggers


async def test_audit_chain_head_seeded(session: AsyncSession) -> None:
    """Migration 0003 creates exactly one ``audit_chain_head`` row."""
    result = await session.execute(text("SELECT count(*) FROM audit_chain_head"))
    assert result.scalar_one() == 1


async def test_recipe_revision_immutable_trigger_present(
    session: AsyncSession,
) -> None:
    result = await session.execute(
        text(
            "SELECT trigger_name FROM information_schema.triggers "
            "WHERE event_object_table = 'recipe_revision'"
        )
    )
    triggers = {row[0] for row in result.all()}
    assert "trg_recipe_revision_immutable" in triggers


async def test_recipe_revision_param_block_trigger_present(
    session: AsyncSession,
) -> None:
    result = await session.execute(
        text(
            "SELECT trigger_name FROM information_schema.triggers "
            "WHERE event_object_table = 'recipe_revision_param'"
        )
    )
    triggers = {row[0] for row in result.all()}
    assert "trg_recipe_revision_param_no_modify" in triggers


async def test_roles_seeded(session: AsyncSession) -> None:
    """All four roles seeded. Compare as a set — enum sorts by declaration order."""
    result = await session.execute(text("SELECT name::text FROM roles"))
    names = {row[0] for row in result.all()}
    assert names == {"admin", "cultivator", "operator", "qap"}


async def test_audit_event_pk_index_unique(session: AsyncSession) -> None:
    """The materialized view's PK index must exist for CONCURRENTLY refresh."""
    result = await session.execute(
        text(
            "SELECT indexname FROM pg_indexes "
            "WHERE tablename = 'effective_target'"
        )
    )
    indexes = {row[0] for row in result.all()}
    assert "effective_target_pk_idx" in indexes


async def test_room_runtime_table_present_after_upgrade(
    session: AsyncSession,
) -> None:
    """``room_runtime`` exists with the columns 0002 + 0005 create."""
    result = await session.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'room_runtime'"
        )
    )
    columns = {row[0] for row in result.all()}
    assert columns == {
        # migration 0002
        "room_id",
        "rollout_stage",
        "cycle_start_date",
        "current_state",
        "muted",
        "last_tick_at",
        "paused",
        # migration 0005 — the room store
        "display_name",
        "equipment_map",
    }


def _alembic_config() -> object:
    """Build an Alembic config pointed at this repo's migration tree."""
    from pathlib import Path  # noqa: PLC0415

    from alembic.config import Config as AlembicConfig  # noqa: PLC0415

    backend_dir = Path(__file__).resolve().parent.parent.parent
    cfg = AlembicConfig(str(backend_dir / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend_dir / "alembic"))
    return cfg


def _room_runtime_exists_sync(database_url: str) -> bool:
    """Synchronously check whether ``room_runtime`` exists.

    Uses a sync ``psycopg`` connection — this runs from a *sync* test so
    it cannot share the async-test event loop, and Alembic's own
    ``env.py`` spins the migration loop itself.
    """
    from sqlalchemy import create_engine  # noqa: PLC0415

    sync_url = database_url.replace("+asyncpg", "+psycopg")
    engine = create_engine(sync_url)
    try:
        with engine.connect() as conn:
            result = conn.execute(
                text(
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_name = 'room_runtime'"
                )
            )
            return result.scalar() == 1
    finally:
        engine.dispose()


@pytest.mark.integration
def test_room_runtime_migration_downgrade(database_url: str) -> None:
    """``alembic downgrade`` to the baseline drops ``room_runtime`` cleanly.

    A *synchronous* test: Alembic's ``env.py`` runs migrations through
    its own ``asyncio.run`` — that cannot be nested inside the
    pytest-asyncio loop an ``async def`` test runs in. So this test runs
    sync. It downgrades to ``0001_baseline`` (exercising migration
    0002's ``downgrade()``) then upgrades back to ``head`` — the net
    schema effect is zero, leaving the session-shared schema intact for
    sibling tests.
    """
    from alembic import command  # noqa: PLC0415

    cfg = _alembic_config()
    try:
        command.downgrade(cfg, "0001_baseline")
        assert not _room_runtime_exists_sync(
            database_url
        ), "room_runtime should be dropped by downgrade"
    finally:
        # Always restore to head so sibling tests see the full schema.
        command.upgrade(cfg, "head")

    assert _room_runtime_exists_sync(
        database_url
    ), "room_runtime should be restored by upgrade"
