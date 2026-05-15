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
