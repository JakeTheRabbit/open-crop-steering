"""SFW approvals API — list, inspect, and decide pending AI proposals.

The UI side of the supervised-approval workflow (the Telegram side is
:mod:`app.workers.telegram_bot`; both feed the same
:mod:`app.core.sfw` lifecycle and produce the same audit rows).

Role gates (plan v3 #7):

* ``GET`` routes — ``operator`` or higher may *view* pending approvals.
* ``POST .../approve`` and ``.../reject`` — ``cultivator`` or higher;
  approval authority is a cultivator capability.

A decision here dispatches into :mod:`app.core.sfw` with
``channel="ui"``; the SFW lifecycle writes exactly one audit row for the
decision (a ``controlled_adjustment`` on a clean approve, a
``guardrail_rejection`` if the re-check fails, an ``info_event`` on a
plain reject).
"""

from __future__ import annotations

from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.acl import require_role
from app.core.auth import Identity
from app.core.sfw import approve_pending, reject_pending
from app.db import get_session
from app.models.pending_approval import PendingApproval, PendingStatus
from app.models.sensor_snapshot import SensorSnapshot
from app.models.user import RoleName

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/approvals", tags=["approvals"])

#: Decision channel recorded on the pending row + audit trail.
_CHANNEL = "ui"


class DecisionBody(BaseModel):
    """Optional payload for an approve / reject request."""

    notes: str | None = Field(
        default=None,
        max_length=1024,
        description="Optional free-text decision note recorded on the row.",
    )


def _pending_summary(pending: PendingApproval) -> dict[str, Any]:
    """Serialize a :class:`PendingApproval` for the list view."""
    return {
        "id": pending.id,
        "room_id": pending.room_id,
        "status": pending.status.value,
        "summary": pending.summary,
        "snapshot_id": pending.snapshot_id,
        "llm_call_id": pending.llm_call_id,
        "created_at": (
            pending.created_at.isoformat() if pending.created_at else None
        ),
        "expires_at": (
            pending.expires_at.isoformat() if pending.expires_at else None
        ),
        "decided_at": (
            pending.decided_at.isoformat() if pending.decided_at else None
        ),
        "decided_by": pending.decided_by,
        "decision_channel": pending.decision_channel,
    }


def _pending_detail(
    pending: PendingApproval, snapshot: SensorSnapshot | None
) -> dict[str, Any]:
    """Serialize a :class:`PendingApproval` with its proposal + snapshot."""
    detail = _pending_summary(pending)
    detail["proposal"] = pending.proposal
    detail["decision_chat_id"] = pending.decision_chat_id
    detail["decision_notes"] = pending.decision_notes
    detail["snapshot"] = (
        {
            "id": snapshot.id,
            "room_id": snapshot.room_id,
            "captured_at": (
                snapshot.captured_at.isoformat()
                if snapshot.captured_at
                else None
            ),
            "recipe_revision_id": snapshot.recipe_revision_id,
            "cycle_day": snapshot.cycle_day,
            "rollout_stage": snapshot.rollout_stage,
            "payload": snapshot.payload,
        }
        if snapshot is not None
        else None
    )
    return detail


@router.get("")
async def list_approvals(
    identity: Annotated[Identity, Depends(require_role(RoleName.operator))],
    session: Annotated[AsyncSession, Depends(get_session)],
    include_decided: Annotated[bool, Query()] = False,
    room_id: str | None = None,
) -> dict[str, Any]:
    """List pending approvals — open by default, newest first.

    Args:
        identity: Authorized caller (``operator`` or higher).
        session: Active async session.
        include_decided: When ``True``, also return approved / rejected /
            expired rows; default lists only ``open`` ones.
        room_id: Optional filter by room.

    Returns:
        ``{"total", "approvals": [...]}``.
    """
    filters: list[Any] = []
    if not include_decided:
        filters.append(PendingApproval.status == PendingStatus.open)
    if room_id is not None:
        filters.append(PendingApproval.room_id == room_id)

    total = await session.scalar(
        select(func.count()).select_from(PendingApproval).where(*filters)
    )
    rows = (
        await session.execute(
            select(PendingApproval)
            .where(*filters)
            .order_by(PendingApproval.id.desc())
        )
    ).scalars()

    log.info(
        "approvals_listed",
        actor=identity.user_id,
        include_decided=include_decided,
    )
    return {
        "total": int(total or 0),
        "approvals": [_pending_summary(p) for p in rows],
    }


@router.get("/{pending_id}")
async def get_approval(
    pending_id: int,
    identity: Annotated[Identity, Depends(require_role(RoleName.operator))],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """Return one pending approval with its proposal + originating snapshot.

    Args:
        pending_id: The approval to fetch.
        identity: Authorized caller (``operator`` or higher).
        session: Active async session.

    Returns:
        The full approval detail dict.

    Raises:
        fastapi.HTTPException: 404 if no such approval exists.
    """
    pending = await session.get(PendingApproval, pending_id)
    if pending is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"pending approval {pending_id} not found",
        )
    snapshot = await session.get(SensorSnapshot, pending.snapshot_id)
    log.info(
        "approval_detail_viewed",
        actor=identity.user_id,
        pending_id=pending_id,
    )
    return _pending_detail(pending, snapshot)


@router.post("/{pending_id}/approve", status_code=status.HTTP_200_OK)
async def approve_approval(
    pending_id: int,
    identity: Annotated[Identity, Depends(require_role(RoleName.cultivator))],
    session: Annotated[AsyncSession, Depends(get_session)],
    body: DecisionBody | None = None,
) -> dict[str, Any]:
    """Approve a pending proposal from the UI (``cultivator`` or higher).

    Dispatches to :func:`app.core.sfw.approve_pending` with
    ``channel="ui"``: the proposal is re-checked and, if it still holds,
    applied as runtime overlays + a command batch. A failing re-check
    rejects the row instead (the response ``status`` then reads
    ``rejected``).

    Args:
        pending_id: The approval to approve.
        identity: Authorized caller (``cultivator`` or higher).
        session: Active async session.
        body: Optional decision note.

    Returns:
        The decided approval summary.

    Raises:
        fastapi.HTTPException: 404 if the approval is unknown; 409 if it
            is not ``open`` (already decided / expired).
    """
    try:
        pending = await approve_pending(
            session,
            pending_id,
            decided_by=identity.user_id,
            channel=_CHANNEL,
            notes=body.notes if body else None,
        )
    except ValueError as exc:
        raise _decision_error(exc) from exc
    await session.commit()

    log.info(
        "approval_approved_via_ui",
        actor=identity.user_id,
        pending_id=pending_id,
        status=pending.status.value,
    )
    return _pending_summary(pending)


@router.post("/{pending_id}/reject", status_code=status.HTTP_200_OK)
async def reject_approval(
    pending_id: int,
    identity: Annotated[Identity, Depends(require_role(RoleName.cultivator))],
    session: Annotated[AsyncSession, Depends(get_session)],
    body: DecisionBody | None = None,
) -> dict[str, Any]:
    """Reject a pending proposal from the UI (``cultivator`` or higher).

    Dispatches to :func:`app.core.sfw.reject_pending` with
    ``channel="ui"`` — no overlay, no command, one audit row.

    Args:
        pending_id: The approval to reject.
        identity: Authorized caller (``cultivator`` or higher).
        session: Active async session.
        body: Optional decision note.

    Returns:
        The decided approval summary.

    Raises:
        fastapi.HTTPException: 404 if the approval is unknown; 409 if it
            is not ``open``.
    """
    try:
        pending = await reject_pending(
            session,
            pending_id,
            decided_by=identity.user_id,
            channel=_CHANNEL,
            notes=body.notes if body else None,
        )
    except ValueError as exc:
        raise _decision_error(exc) from exc
    await session.commit()

    log.info(
        "approval_rejected_via_ui",
        actor=identity.user_id,
        pending_id=pending_id,
    )
    return _pending_summary(pending)


def _decision_error(exc: ValueError) -> HTTPException:
    """Map an :mod:`app.core.sfw` ``ValueError`` to the right HTTP status.

    A missing row is a 404; an already-decided row is a 409 conflict
    (decisions are one-shot). The exception message already names the
    offending pending id.
    """
    message = str(exc)
    if "not found" in message:
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=message
        )
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=message)
