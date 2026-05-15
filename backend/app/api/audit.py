"""Audit-log API — list, verify, and export the tamper-evident chain.

All routes require the ``qap`` role or higher (plan v3 #7: QAP owns
audit export and chain verification). ``operator``/``cultivator`` see
audit data only indirectly through other UI.

Every export writes an ``audit_export`` audit event so the act of
exporting is itself recorded in the chain (plan locked decision #6).

Export formats:

* ``csv`` — a real, streaming CSV of the requested range.
* ``pdf`` — returns **HTTP 501**. A styled PDF inspector report is
  scheduled for **P13** (docs / validation pack) where the validation
  templates and branding live; emitting an unstyled stub now would
  create a throwaway artifact. ``csv`` covers the machine-readable
  inspector need until then.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
from typing import Annotated, Any, Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.acl import require_role
from app.core.audit import log_audit
from app.core.auth import Identity
from app.core.seal import verify_chain
from app.db import get_session
from app.models.audit_event import AuditEvent, AuditEventType
from app.models.user import RoleName

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/audit", tags=["audit"])


def _row_to_dict(event: AuditEvent) -> dict[str, Any]:
    """Serialize an :class:`AuditEvent` for JSON / CSV output.

    Binary chain fields are hex-encoded so the row is plain text.
    """
    return {
        "id": event.id,
        "occurred_at": event.occurred_at.isoformat() if event.occurred_at else None,
        "event_type": event.event_type.value,
        "actor_id": event.actor_id,
        "actor_role": event.actor_role,
        "room_id": event.room_id,
        "summary": event.summary,
        "params": event.params,
        "reason_codes": event.reason_codes,
        "recipe_revision_id": event.recipe_revision_id,
        "runtime_adjustment_id": event.runtime_adjustment_id,
        "command_id": event.command_id,
        "snapshot_id": event.snapshot_id,
        "llm_call_id": event.llm_call_id,
        "key_id": event.key_id,
        "prev_event_hash": bytes(event.prev_event_hash).hex(),
        "hmac": bytes(event.hmac).hex(),
    }


def _range_clause(
    start: dt.datetime | None, end: dt.datetime | None
) -> list[Any]:
    """Build SQLAlchemy filter clauses for an ``occurred_at`` range."""
    clauses: list[Any] = []
    if start is not None:
        clauses.append(AuditEvent.occurred_at >= start)
    if end is not None:
        clauses.append(AuditEvent.occurred_at <= end)
    return clauses


@router.get("/events")
async def list_events(
    identity: Annotated[Identity, Depends(require_role(RoleName.qap))],
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
    event_type: AuditEventType | None = None,
    room_id: str | None = None,
) -> dict[str, Any]:
    """Return a paginated slice of the audit log (newest first).

    Args:
        identity: Authorized caller (``qap`` or higher).
        session: Active async session.
        limit: Page size (1-500).
        offset: Rows to skip.
        event_type: Optional filter by event type.
        room_id: Optional filter by room.

    Returns:
        ``{"total", "limit", "offset", "events": [...]}``.
    """
    filters: list[Any] = []
    if event_type is not None:
        filters.append(AuditEvent.event_type == event_type)
    if room_id is not None:
        filters.append(AuditEvent.room_id == room_id)

    total = await session.scalar(
        select(func.count()).select_from(AuditEvent).where(*filters)
    )
    rows = (
        await session.execute(
            select(AuditEvent)
            .where(*filters)
            .order_by(AuditEvent.id.desc())
            .limit(limit)
            .offset(offset)
        )
    ).scalars()

    log.info(
        "audit_events_listed",
        actor=identity.user_id,
        limit=limit,
        offset=offset,
    )
    return {
        "total": int(total or 0),
        "limit": limit,
        "offset": offset,
        "events": [_row_to_dict(e) for e in rows],
    }


@router.get("/verify")
async def verify(
    identity: Annotated[Identity, Depends(require_role(RoleName.qap))],
    session: Annotated[AsyncSession, Depends(get_session)],
    start_id: Annotated[int | None, Query(ge=1)] = None,
    end_id: Annotated[int | None, Query(ge=1)] = None,
) -> dict[str, Any]:
    """Run a tamper-evidence verification of the audit chain.

    Recomputes every row's HMAC server-side and checks chain linkage
    (see :func:`app.core.seal.verify_chain`). A failure here is a
    formal-deviation-grade finding.

    Args:
        identity: Authorized caller (``qap`` or higher).
        session: Active async session.
        start_id: Optional inclusive lower id bound.
        end_id: Optional inclusive upper id bound.

    Returns:
        ``{"ok", "rows_checked", "first_bad_id", "message"}``.
    """
    result = await verify_chain(session, start_id=start_id, end_id=end_id)
    log.info(
        "audit_chain_verified",
        actor=identity.user_id,
        ok=result.ok,
        rows_checked=result.rows_checked,
        first_bad_id=result.first_bad_id,
    )
    return {
        "ok": result.ok,
        "rows_checked": result.rows_checked,
        "first_bad_id": result.first_bad_id,
        "message": result.message,
    }


def _csv_response(
    events: list[AuditEvent], start: dt.datetime | None, end: dt.datetime | None
) -> StreamingResponse:
    """Render *events* as a downloadable CSV streaming response."""
    fields = [
        "id",
        "occurred_at",
        "event_type",
        "actor_id",
        "actor_role",
        "room_id",
        "summary",
        "reason_codes",
        "key_id",
        "prev_event_hash",
        "hmac",
    ]
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for event in events:
        row = _row_to_dict(event)
        row["reason_codes"] = ",".join(row.get("reason_codes") or [])
        writer.writerow({k: row.get(k) for k in fields})
    buffer.seek(0)

    epoch = dt.datetime(1970, 1, 1, tzinfo=dt.UTC)
    span = f"{(start or epoch).date()}_{(end or dt.datetime.now(dt.UTC)).date()}"
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="audit-{span}.csv"'
        },
    )


@router.get("/export")
async def export_audit(
    identity: Annotated[Identity, Depends(require_role(RoleName.qap))],
    session: Annotated[AsyncSession, Depends(get_session)],
    export_format: Annotated[Literal["csv", "pdf"], Query(alias="format")] = "csv",
    start: dt.datetime | None = None,
    end: dt.datetime | None = None,
) -> StreamingResponse:
    """Export the audit log over a time range.

    Writes an ``audit_export`` audit event recording the request, then
    streams the data. CSV is fully supported; PDF returns HTTP 501
    (see the module docstring — styled PDF is a P13 deliverable).

    Args:
        identity: Authorized caller (``qap`` or higher).
        session: Active async session.
        export_format: ``csv`` (real) or ``pdf`` (501 — P13).
        start: Optional inclusive lower bound on ``occurred_at``.
        end: Optional inclusive upper bound on ``occurred_at``.

    Returns:
        A CSV :class:`StreamingResponse`.

    Raises:
        fastapi.HTTPException: 501 when ``format=pdf``.
    """
    if export_format == "pdf":
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=(
                "PDF audit export is not implemented; use format=csv. "
                "Styled PDF inspector report is scheduled for P13."
            ),
        )

    rows = list(
        (
            await session.execute(
                select(AuditEvent)
                .where(*_range_clause(start, end))
                .order_by(AuditEvent.id)
            )
        ).scalars()
    )

    # Record the export in the chain itself.
    await log_audit(
        session,
        event_type=AuditEventType.audit_export,
        actor_id=identity.user_id,
        actor_role=identity.mode,
        summary=(
            f"Audit export ({export_format}, {len(rows)} rows) by {identity.user_id}"
        ),
        params={
            "format": export_format,
            "start": start.isoformat() if start else None,
            "end": end.isoformat() if end else None,
            "row_count": len(rows),
        },
    )
    await session.commit()

    log.info(
        "audit_exported",
        actor=identity.user_id,
        format=export_format,
        rows=len(rows),
    )
    return _csv_response(rows, start, end)
