"""Rollout + formal-deviation API.

The QAP-facing surface for the Phase-9 rollout-advance and
formal-deviation lifecycle:

* ``GET  /api/rollout`` — every room's rollout state + advance-gate
  status. ``operator`` or higher may view.
* ``POST /api/rollout/{room_id}/advance`` — advance a room one rollout
  stage. ``qap`` or higher (plan v3 #7: rollout advancement is a QAP
  capability); blocked by any unresolved formal deviation (REQ-010).
* ``GET  /api/deviations`` — the open (unacknowledged) formal
  deviations. ``operator`` or higher may view.
* ``POST /api/deviations/{event_id}/ack`` — acknowledge a formal
  deviation, clearing the rollout block. ``qap`` or higher.

Both write paths dispatch into :mod:`app.core.rollout` /
:mod:`app.core.deviations`, which write the corresponding audit rows
(``rollout_advanced`` / ``deviation_acknowledged``) so the QAP actions
are part of the tamper-evident chain.
"""

from __future__ import annotations

from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.acl import require_role
from app.core.auth import Identity
from app.core.deviations import acknowledge_deviation, list_open_deviations
from app.core.rollout import (
    ROLLOUT_STAGES,
    AdvanceCheck,
    advance_stage,
    can_advance,
)
from app.db import get_session
from app.models.event_log import EventLogEntry
from app.models.room_runtime import RoomRuntime
from app.models.user import RoleName

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api", tags=["rollout"])


# --- request bodies ----------------------------------------------------


class AdvanceBody(BaseModel):
    """Optional payload for a rollout-advance request."""

    notes: str | None = Field(
        default=None,
        max_length=1024,
        description="Optional free-text note recorded with the advance.",
    )


class AckBody(BaseModel):
    """Optional payload for a deviation-acknowledge request."""

    notes: str | None = Field(
        default=None,
        max_length=1024,
        description=(
            "Optional acknowledgement note (e.g. the corrective action "
            "taken), recorded in the audit trail."
        ),
    )


# --- serializers -------------------------------------------------------


def _advance_check_dict(check: AdvanceCheck) -> dict[str, Any]:
    """Serialize an :class:`AdvanceCheck` for the API."""
    return {
        "room_id": check.room_id,
        "can_advance": check.can_advance,
        "current_stage": {
            "index": check.current_stage.index,
            "name": check.current_stage.name,
            "description": check.current_stage.description,
        },
        "next_stage": (
            {
                "index": check.next_stage.index,
                "name": check.next_stage.name,
                "description": check.next_stage.description,
            }
            if check.next_stage is not None
            else None
        ),
        "no_open_deviations": check.no_open_deviations,
        "open_deviation_count": check.open_deviation_count,
        "sfw_rejections_7d": check.sfw_rejections_7d,
        "sfw_rejection_gate_met": check.sfw_rejection_gate_met,
        "min_days_at_stage_met": check.min_days_at_stage_met,
        "qap_approval_recorded": check.qap_approval_recorded,
        "blockers": check.blockers,
    }


def _deviation_dict(event: EventLogEntry) -> dict[str, Any]:
    """Serialize a formal-deviation :class:`EventLogEntry` for the API."""
    return {
        "id": event.id,
        "room_id": event.room_id,
        "occurred_at": (
            event.occurred_at.isoformat() if event.occurred_at else None
        ),
        "severity": event.severity.value,
        "summary": event.summary,
        "reason_codes": list(event.reason_codes or []),
        "payload": event.payload,
        "acknowledged_by": event.acknowledged_by,
        "acknowledged_at": (
            event.acknowledged_at.isoformat()
            if event.acknowledged_at
            else None
        ),
        "audit_event_id": event.audit_event_id,
    }


# --- rollout state -----------------------------------------------------


@router.get("/rollout")
async def get_rollout(
    identity: Annotated[Identity, Depends(require_role(RoleName.operator))],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """Return every room's rollout state + advance-gate status.

    For each room that has a ``room_runtime`` row, runs
    :func:`app.core.rollout.can_advance` so the UI can show whether the
    room is eligible to advance and, if not, why.

    Args:
        identity: Authorized caller (``operator`` or higher).
        session: Active async session.

    Returns:
        ``{"stages": [...], "rooms": [...]}`` — the rollout ladder plus a
        per-room advance-check.
    """
    rooms = (
        await session.execute(select(RoomRuntime).order_by(RoomRuntime.room_id))
    ).scalars()
    room_states: list[dict[str, Any]] = []
    for runtime in rooms:
        check = await can_advance(session, runtime.room_id)
        entry = _advance_check_dict(check)
        entry["paused"] = runtime.paused
        entry["muted"] = runtime.muted
        entry["current_state"] = runtime.current_state
        room_states.append(entry)

    log.info("rollout_state_listed", actor=identity.user_id, rooms=len(room_states))
    return {
        "stages": [
            {"index": s.index, "name": s.name, "description": s.description}
            for s in ROLLOUT_STAGES
        ],
        "rooms": room_states,
    }


@router.post("/rollout/{room_id}/advance", status_code=status.HTTP_200_OK)
async def advance_rollout(
    room_id: str,
    identity: Annotated[Identity, Depends(require_role(RoleName.qap))],
    session: Annotated[AsyncSession, Depends(get_session)],
    body: AdvanceBody | None = None,  # noqa: ARG001 — note reserved for audit
) -> dict[str, Any]:
    """Advance a room one rollout stage — ``qap`` or higher.

    Dispatches to :func:`app.core.rollout.advance_stage`. The advance is
    blocked (HTTP 409) if the room has any unresolved formal deviation
    (REQ-010) or is already at the final stage. A successful advance
    writes a ``rollout_advanced`` audit row with the calling QAP as the
    actor.

    Args:
        room_id: The room to advance.
        identity: Authorized caller (``qap`` or higher).
        session: Active async session.
        body: Optional advance note.

    Returns:
        The post-advance :class:`~app.core.rollout.AdvanceCheck` for the
        room.

    Raises:
        fastapi.HTTPException: 409 if the room cannot advance (the detail
            lists every blocker).
    """
    try:
        await advance_stage(session, room_id, qap_user=identity.user_id)
    except ValueError as exc:
        log.warning(
            "rollout_advance_blocked",
            actor=identity.user_id,
            room_id=room_id,
            error=str(exc),
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    await session.commit()

    check = await can_advance(session, room_id)
    log.info(
        "rollout_advanced_via_api",
        actor=identity.user_id,
        room_id=room_id,
        new_stage=check.current_stage.name,
    )
    return _advance_check_dict(check)


# --- deviations --------------------------------------------------------


@router.get("/deviations")
async def get_deviations(
    identity: Annotated[Identity, Depends(require_role(RoleName.operator))],
    session: Annotated[AsyncSession, Depends(get_session)],
    room_id: str | None = None,
) -> dict[str, Any]:
    """List the open (unacknowledged) formal deviations.

    Args:
        identity: Authorized caller (``operator`` or higher).
        session: Active async session.
        room_id: Optional room filter.

    Returns:
        ``{"total", "deviations": [...]}`` — open deviations, newest
        first.
    """
    rows = await list_open_deviations(session, room_id)
    log.info(
        "deviations_listed",
        actor=identity.user_id,
        room_id=room_id,
        count=len(rows),
    )
    return {
        "total": len(rows),
        "deviations": [_deviation_dict(r) for r in rows],
    }


@router.post("/deviations/{event_id}/ack", status_code=status.HTTP_200_OK)
async def ack_deviation(
    event_id: int,
    identity: Annotated[Identity, Depends(require_role(RoleName.qap))],
    session: Annotated[AsyncSession, Depends(get_session)],
    body: AckBody | None = None,
) -> dict[str, Any]:
    """Acknowledge a formal deviation — ``qap`` or higher.

    Dispatches to :func:`app.core.deviations.acknowledge_deviation`:
    clears the deviation (so it no longer blocks rollout advancement for
    its room) and writes a ``deviation_acknowledged`` audit row.

    Args:
        event_id: The ``event_log`` id of the deviation to acknowledge.
        identity: Authorized caller (``qap`` or higher).
        session: Active async session.
        body: Optional acknowledgement note.

    Returns:
        The acknowledged deviation dict.

    Raises:
        fastapi.HTTPException: 404 if the event is unknown / not a
            deviation; 409 if it is already acknowledged.
    """
    try:
        event = await acknowledge_deviation(
            session,
            event_id,
            qap_user=identity.user_id,
            notes=body.notes if body else None,
        )
    except ValueError as exc:
        message = str(exc)
        if "not found" in message or "not a formal_deviation" in message:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=message
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=message
        ) from exc
    await session.commit()

    log.info(
        "deviation_acked_via_api",
        actor=identity.user_id,
        event_id=event_id,
    )
    return _deviation_dict(event)
