"""High-level helper for inserting audit events.

The actual HMAC chain + ``prev_event_hash`` are computed by the
``trg_audit_event_hmac_chain`` BEFORE INSERT trigger (see baseline
migration). This helper just builds the row with the current
``key_id`` and lets Postgres do the rest.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.audit_event import AuditEvent, AuditEventType


async def log_audit(
    session: AsyncSession,
    *,
    event_type: AuditEventType,
    actor_id: str | None,
    actor_role: str | None = None,
    room_id: str | None = None,
    summary: str | None = None,
    params: dict[str, Any] | None = None,
    reason_codes: list[str] | None = None,
    recipe_revision_id: int | None = None,
    runtime_adjustment_id: int | None = None,
    command_id: int | None = None,
    snapshot_id: int | None = None,
    llm_call_id: int | None = None,
) -> AuditEvent:
    """Insert one audit row.

    The trigger populates ``prev_event_hash`` and ``hmac``; the empty
    bytes we pass are placeholders that get overwritten before the row
    actually lands.
    """
    settings = get_settings()
    event = AuditEvent(
        event_type=event_type,
        actor_id=actor_id,
        actor_role=actor_role,
        room_id=room_id,
        summary=summary,
        params=params or {},
        reason_codes=reason_codes or [],
        recipe_revision_id=recipe_revision_id,
        runtime_adjustment_id=runtime_adjustment_id,
        command_id=command_id,
        snapshot_id=snapshot_id,
        llm_call_id=llm_call_id,
        key_id=settings.hmac_key_id_current,
        prev_event_hash=b"\x00" * 32,  # overwritten by trigger
        hmac=b"\x00" * 32,  # overwritten by trigger
    )
    session.add(event)
    await session.flush()
    await session.refresh(event)
    return event
