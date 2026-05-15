"""SFW (Supervised-approval) pending lifecycle.

When a room's effective mode for a parameter class is
``supervised_approval`` (plan locked decision #10/#11), an AI proposal is
**not** auto-applied. Instead the supervisor turns it into a
:class:`~app.models.pending_approval.PendingApproval` row — routed to
Telegram and the UI — and a human with the ``cultivator`` role (or
higher) approves or rejects it. Only on approval does the proposal
become a runtime overlay + a command-queue batch.

This module owns that lifecycle:

* :func:`create_pending` — open a pending row from an AI proposal.
* :func:`approve_pending` — re-check, then (if it still passes) apply the
  proposal as overlays + a command batch and mark it approved.
* :func:`reject_pending` — mark a pending row rejected.
* :func:`expire_stale_pending` — sweep open rows past their TTL.
* :func:`recheck_proposal` — the basic consistency re-check run on
  approve.

Decision channel
----------------
Either the UI (``channel="ui"``) or Telegram (``channel="telegram"``)
can drive an approve/reject; the decision logic is identical and writes
**one** audit row regardless of channel (the channel + chat_id are
recorded on the row and in the audit ``params``). Telegram approvals are
authoritative only when the originating chat_id maps to an HA user with
the right role — that verification lives in
:mod:`app.workers.telegram_bot`, which calls into here once the chat_id
is resolved.

The proposal payload
--------------------
``PendingApproval.proposal`` is the :class:`~app.schemas.llm_decision.LLMDecision`
serialised to a dict, plus a ``day_index`` key the supervisor stamps in
(the cycle day the snapshot was for). Each entry of ``proposed_changes``
becomes one :func:`app.core.overlays.add_adjustment` overlay on apply;
the resulting effective targets are written to HA through one
:func:`app.core.command_queue.enqueue_batch`.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import select, update

from app.core.audit import log_audit
from app.core.command_queue import enqueue_batch
from app.core.effective import effective_target
from app.core.overlays import add_adjustment
from app.models.audit_event import AuditEventType
from app.models.pending_approval import PendingApproval, PendingStatus
from app.models.recipe_revision import RecipeRevision, RecipeStatus
from app.models.runtime_adjustment import AdjustmentMode, AdjustmentSource
from app.models.sensor_snapshot import SensorSnapshot

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger(__name__)

#: Default time-to-live for a pending approval (plan: 90-min TTL).
DEFAULT_TTL_MINUTES = 90

#: Worker identity used when the supervisor opens a pending row.
_SUPERVISOR_ACTOR = "supervisor"


@dataclass(slots=True)
class RecheckResult:
    """The outcome of :func:`recheck_proposal`.

    Attributes:
        ok: ``True`` if the proposal is still safe to apply.
        reason: Short machine-ish reason when ``ok`` is ``False`` (empty
            on success).
        detail: Human-readable explanation (empty on success).
    """

    ok: bool
    reason: str = ""
    detail: str = ""


@dataclass(slots=True)
class ProposedChange:
    """One concrete parameter change extracted from a stored proposal.

    Attributes:
        param_name: The setpoint the change targets.
        delta: Signed delta to add as an overlay.
        day_index: Cycle day the overlay applies to.
        reason_codes: Reason codes carried from the proposal.
    """

    param_name: str
    delta: float
    day_index: int
    reason_codes: list[str] = field(default_factory=list)


def _extract_changes(proposal: dict[str, Any]) -> list[ProposedChange]:
    """Pull the applicable, non-zero changes out of a stored proposal.

    ``no_change`` entries and zero-delta entries are dropped — they would
    add a meaningless overlay. The ``day_index`` is taken from the
    proposal envelope (the supervisor stamps the snapshot's cycle day).

    Args:
        proposal: The :class:`PendingApproval.proposal` dict.

    Returns:
        One :class:`ProposedChange` per change worth applying.
    """
    day_index = int(proposal.get("day_index", 1))
    reason_codes = [str(rc) for rc in proposal.get("reason_codes", [])]
    changes: list[ProposedChange] = []
    for raw in proposal.get("proposed_changes", []):
        if not isinstance(raw, dict):
            continue
        delta = raw.get("delta")
        if not isinstance(delta, (int, float)):
            continue
        if raw.get("direction") == "no_change" or float(delta) == 0.0:
            continue
        changes.append(
            ProposedChange(
                param_name=str(raw["param_name"]),
                delta=float(delta),
                day_index=day_index,
                reason_codes=reason_codes,
            )
        )
    return changes


def recheck_proposal(
    snapshot: SensorSnapshot | None,
    pending: PendingApproval,
    active_revision: RecipeRevision | None,
) -> RecheckResult:
    """Basic consistency re-check, run when a pending approval is decided.

    The room's state may have shifted between the supervisor building the
    snapshot and a human getting around to approving the pending row.
    This Phase-8 re-check is deliberately *basic* — it confirms only that:

    1. the originating :class:`SensorSnapshot` still exists;
    2. the room's active approved ``recipe_revision`` is the **same**
       revision the snapshot was taken against (a mid-flight recipe
       revision invalidates the proposal — plan risk #4);
    3. every proposed ``delta`` is a finite number.

    .. note::
       **Phase 9 plugs the full guardrail / anti-pattern validator into
       this same function.** Phase 9's
       ``guardrails.validate_all(proposal, snapshot, cumulative_state)``
       — cumulative-delta caps, cool-downs, no-touch windows, the
       anti-pattern (AP-01..AP-12) and coupling-rule (EC-001..EC-015)
       checks — belongs here, extending (not replacing) the three basic
       checks below. The lifecycle in :func:`approve_pending` already
       treats a failing re-check as a rejection, so Phase 9 only has to
       widen what counts as a failure.

    Args:
        snapshot: The :class:`SensorSnapshot` the proposal was built from
            (``None`` if it has since been deleted).
        pending: The pending approval being decided.
        active_revision: The room's current active approved revision
            (``None`` if the room has none).

    Returns:
        A :class:`RecheckResult` — ``ok`` true when the proposal may
        still be applied.
    """
    if snapshot is None:
        return RecheckResult(
            ok=False,
            reason="snapshot_missing",
            detail=(
                f"snapshot {pending.snapshot_id} no longer exists; "
                "cannot re-validate the proposal"
            ),
        )

    if active_revision is None:
        return RecheckResult(
            ok=False,
            reason="no_active_revision",
            detail=f"room {pending.room_id} has no active approved recipe revision",
        )

    if active_revision.id != snapshot.recipe_revision_id:
        return RecheckResult(
            ok=False,
            reason="recipe_revision_changed",
            detail=(
                f"recipe revision changed since the snapshot "
                f"(snapshot={snapshot.recipe_revision_id}, "
                f"active={active_revision.id})"
            ),
        )

    for change in _extract_changes(pending.proposal):
        if not math.isfinite(change.delta):
            return RecheckResult(
                ok=False,
                reason="non_finite_delta",
                detail=(
                    f"proposed delta for {change.param_name} is not a "
                    "finite number"
                ),
            )

    return RecheckResult(ok=True)


async def _active_revision(
    session: AsyncSession, room_id: str
) -> RecipeRevision | None:
    """Return the highest-version approved revision for a room."""
    result = await session.execute(
        select(RecipeRevision)
        .where(
            RecipeRevision.room_id == room_id,
            RecipeRevision.status == RecipeStatus.approved,
        )
        .order_by(RecipeRevision.version.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def create_pending(
    session: AsyncSession,
    *,
    room_id: str,
    proposal: dict[str, Any],
    snapshot_id: int,
    llm_call_id: int | None,
    summary: str,
    ttl_minutes: int = DEFAULT_TTL_MINUTES,
) -> PendingApproval:
    """Open a pending approval from an AI proposal (SFW mode).

    Called by the supervisor when a room's effective mode for a class is
    ``supervised_approval`` and the LLM returned a non-empty proposal.
    The proposal is **not** applied — it is parked here for a human.

    Args:
        session: Active async session.
        room_id: Room the proposal is for.
        proposal: The :class:`~app.schemas.llm_decision.LLMDecision` as a
            dict, with a ``day_index`` key stamped in by the supervisor.
        snapshot_id: The :class:`SensorSnapshot` the proposal was built
            from.
        llm_call_id: The ``llm_call_log`` row id, if known.
        summary: Short human-facing summary (shown in Telegram / the UI).
        ttl_minutes: Minutes until the pending row auto-expires.

    Returns:
        The persisted, ``open`` :class:`PendingApproval`.
    """
    now = dt.datetime.now(dt.UTC)
    pending = PendingApproval(
        room_id=room_id,
        proposal=proposal,
        snapshot_id=snapshot_id,
        llm_call_id=llm_call_id,
        summary=summary[:1024],
        status=PendingStatus.open,
        expires_at=now + dt.timedelta(minutes=ttl_minutes),
    )
    session.add(pending)
    await session.flush()  # assigns pending.id

    await log_audit(
        session,
        event_type=AuditEventType.info_event,
        actor_id=_SUPERVISOR_ACTOR,
        room_id=room_id,
        summary=f"SFW pending approval #{pending.id} opened for {room_id}",
        params={
            "pending_id": pending.id,
            "mode": AdjustmentMode.supervised_approval.value,
            "expires_at": pending.expires_at.isoformat(),
            "proposed_change_count": len(_extract_changes(proposal)),
        },
        reason_codes=[str(rc) for rc in proposal.get("reason_codes", [])],
        snapshot_id=snapshot_id,
        llm_call_id=llm_call_id,
    )

    log.info(
        "sfw_pending_created",
        pending_id=pending.id,
        room_id=room_id,
        snapshot_id=snapshot_id,
        expires_at=pending.expires_at.isoformat(),
    )
    return pending


def _idempotency_key(pending: PendingApproval) -> str:
    """Stable command-batch key for a pending approval's apply.

    Keyed on the pending id so a duplicated approve (UI double-tap, a
    Telegram callback re-delivery) cannot enqueue the setpoint write
    twice — :func:`app.core.command_queue.enqueue_batch` is idempotent on
    this key.
    """
    return f"sfw-approval:{pending.id}"


async def _apply_proposal(
    session: AsyncSession,
    pending: PendingApproval,
    *,
    decided_by: str,
) -> tuple[int, int]:
    """Apply an approved proposal as overlays + one command batch.

    Each :class:`ProposedChange` becomes a ``supervised_approval`` runtime
    overlay (source ``ai_sfw``); the resulting effective targets are then
    written to HA through a single idempotent command batch.

    Args:
        session: Active async session.
        pending: The pending approval being applied.
        decided_by: ``users.id`` of the approver (the overlay's
            ``created_by``).

    Returns:
        ``(overlay_count, command_count)`` actually enqueued.
    """
    changes = _extract_changes(pending.proposal)
    # Overlays expire at end of the current cultivation day by default —
    # the same "expire at lights-off" posture overlays use elsewhere.
    expires_at = dt.datetime.now(dt.UTC) + dt.timedelta(hours=12)

    commands: list[dict[str, Any]] = []
    for change in changes:
        await add_adjustment(
            session,
            room_id=pending.room_id,
            day_index=change.day_index,
            param_name=change.param_name,
            delta=change.delta,
            source=AdjustmentSource.ai_sfw,
            mode=AdjustmentMode.supervised_approval,
            expires_at=expires_at,
            created_by=decided_by,
            snapshot_id=pending.snapshot_id,
            reason_codes=change.reason_codes,
        )
        # add_adjustment refreshes the effective_target matview, so the
        # post-overlay value is now readable.
        value = await effective_target(
            session, pending.room_id, change.day_index, change.param_name
        )
        if value is None:
            log.warning(
                "sfw_apply_no_effective_target",
                pending_id=pending.id,
                room_id=pending.room_id,
                param_name=change.param_name,
            )
            continue
        entity = f"input_number.{pending.room_id}_setpoint_{change.param_name}"
        commands.append(
            {
                "domain": "input_number",
                "service": "set_value",
                "target_entity": entity,
                "service_data": {"entity_id": entity, "value": value},
                "expected_value": f"{value}",
                "reason": f"SFW approval #{pending.id} ({change.param_name})",
            }
        )

    if commands:
        await enqueue_batch(
            session,
            room_id=pending.room_id,
            purpose="sfw_approval",
            idempotency_key=_idempotency_key(pending),
            commands=commands,
            enqueued_by=decided_by,
        )

    return len(changes), len(commands)


async def _decided_pending(
    session: AsyncSession, pending_id: int
) -> PendingApproval:
    """Load an ``open`` pending row for a decision, or raise.

    Raises:
        ValueError: If the pending row does not exist or is not ``open``
            (already decided / expired — decisions are one-shot).
    """
    pending = await session.get(PendingApproval, pending_id)
    if pending is None:
        raise ValueError(f"pending approval {pending_id} not found")
    if pending.status is not PendingStatus.open:
        raise ValueError(
            f"pending approval {pending_id} is {pending.status.value}, "
            "not open — it has already been decided"
        )
    return pending


def _stamp_decision(
    pending: PendingApproval,
    *,
    status: PendingStatus,
    decided_by: str,
    channel: str,
    chat_id: str | None,
    notes: str | None,
) -> None:
    """Write the common decision fields onto a pending row."""
    pending.status = status
    pending.decided_at = dt.datetime.now(dt.UTC)
    pending.decided_by = decided_by
    pending.decision_channel = channel
    pending.decision_chat_id = chat_id
    pending.decision_notes = notes[:1024] if notes else None


async def approve_pending(
    session: AsyncSession,
    pending_id: int,
    *,
    decided_by: str,
    channel: str,
    chat_id: str | None = None,
    notes: str | None = None,
    ha_client: Any = None,  # noqa: ARG001 — reserved for a future live readback
) -> PendingApproval:
    """Approve a pending proposal — re-check, then apply if it still holds.

    The flow:

    1. Load the ``open`` pending row (a non-open row is a one-shot-decision
       error).
    2. Run :func:`recheck_proposal` — the room state may have shifted
       since the snapshot.
    3. **Re-check passes** → apply the proposal (overlays +
       command batch via :func:`_apply_proposal`), mark the row
       ``approved``, write one ``controlled_adjustment`` audit row.
    4. **Re-check fails** → the proposal is *not* applied; the row is
       marked ``rejected`` with the re-check reason and a
       ``guardrail_rejection`` audit row is written instead.

    Exactly one audit row is written regardless of the decision channel
    (UI or Telegram); the channel and chat_id are recorded on the pending
    row and in the audit ``params``.

    Args:
        session: Active async session.
        pending_id: The pending approval to approve.
        decided_by: ``users.id`` of the approver (already role-checked by
            the caller).
        channel: ``"ui"`` or ``"telegram"``.
        chat_id: Telegram chat_id when ``channel == "telegram"``; ``None``
            for the UI.
        notes: Optional free-text decision note.
        ha_client: Reserved for a future synchronous readback; unused in
            Phase 8 (the executor worker performs the HA write + readback
            off the command queue).

    Returns:
        The decided :class:`PendingApproval` — ``approved`` if applied,
        ``rejected`` if the re-check failed.

    Raises:
        ValueError: If the pending row does not exist or is not ``open``.
    """
    pending = await _decided_pending(session, pending_id)

    snapshot = await session.get(SensorSnapshot, pending.snapshot_id)
    active_revision = await _active_revision(session, pending.room_id)
    recheck = recheck_proposal(snapshot, pending, active_revision)

    if not recheck.ok:
        # Re-check failed: keep the row but reject it with the reason.
        _stamp_decision(
            pending,
            status=PendingStatus.rejected,
            decided_by=decided_by,
            channel=channel,
            chat_id=chat_id,
            notes=f"re-check failed: {recheck.detail}"
            + (f" | {notes}" if notes else ""),
        )
        await session.flush()
        await log_audit(
            session,
            event_type=AuditEventType.guardrail_rejection,
            actor_id=decided_by,
            room_id=pending.room_id,
            summary=(
                f"SFW approval #{pending.id} rejected on re-check: "
                f"{recheck.reason}"
            ),
            params={
                "pending_id": pending.id,
                "decision": "rejected_on_recheck",
                "channel": channel,
                "chat_id": chat_id,
                "recheck_reason": recheck.reason,
                "recheck_detail": recheck.detail,
            },
            reason_codes=[recheck.reason],
            snapshot_id=pending.snapshot_id,
            llm_call_id=pending.llm_call_id,
        )
        log.warning(
            "sfw_approval_rejected_on_recheck",
            pending_id=pending.id,
            room_id=pending.room_id,
            reason=recheck.reason,
        )
        return pending

    overlay_count, command_count = await _apply_proposal(
        session, pending, decided_by=decided_by
    )
    _stamp_decision(
        pending,
        status=PendingStatus.approved,
        decided_by=decided_by,
        channel=channel,
        chat_id=chat_id,
        notes=notes,
    )
    await session.flush()

    await log_audit(
        session,
        event_type=AuditEventType.controlled_adjustment,
        actor_id=decided_by,
        room_id=pending.room_id,
        summary=(
            f"SFW approval #{pending.id} approved via {channel}: "
            f"{overlay_count} overlay(s), {command_count} command(s) enqueued"
        ),
        params={
            "pending_id": pending.id,
            "decision": "approved",
            "channel": channel,
            "chat_id": chat_id,
            "overlay_count": overlay_count,
            "command_count": command_count,
        },
        reason_codes=[str(rc) for rc in pending.proposal.get("reason_codes", [])],
        snapshot_id=pending.snapshot_id,
        llm_call_id=pending.llm_call_id,
    )
    log.info(
        "sfw_approval_approved",
        pending_id=pending.id,
        room_id=pending.room_id,
        channel=channel,
        overlay_count=overlay_count,
        command_count=command_count,
    )
    return pending


async def reject_pending(
    session: AsyncSession,
    pending_id: int,
    *,
    decided_by: str,
    channel: str,
    chat_id: str | None = None,
    notes: str | None = None,
) -> PendingApproval:
    """Reject a pending proposal — no overlay, no command, audit-rowed.

    Args:
        session: Active async session.
        pending_id: The pending approval to reject.
        decided_by: ``users.id`` of the rejecter (role-checked by caller).
        channel: ``"ui"`` or ``"telegram"``.
        chat_id: Telegram chat_id when ``channel == "telegram"``.
        notes: Optional free-text decision note.

    Returns:
        The ``rejected`` :class:`PendingApproval`.

    Raises:
        ValueError: If the pending row does not exist or is not ``open``.
    """
    pending = await _decided_pending(session, pending_id)
    _stamp_decision(
        pending,
        status=PendingStatus.rejected,
        decided_by=decided_by,
        channel=channel,
        chat_id=chat_id,
        notes=notes,
    )
    await session.flush()

    await log_audit(
        session,
        event_type=AuditEventType.info_event,
        actor_id=decided_by,
        room_id=pending.room_id,
        summary=f"SFW approval #{pending.id} rejected via {channel}",
        params={
            "pending_id": pending.id,
            "decision": "rejected",
            "channel": channel,
            "chat_id": chat_id,
        },
        snapshot_id=pending.snapshot_id,
        llm_call_id=pending.llm_call_id,
    )
    log.info(
        "sfw_approval_rejected",
        pending_id=pending.id,
        room_id=pending.room_id,
        channel=channel,
    )
    return pending


async def expire_stale_pending(session: AsyncSession) -> int:
    """Expire every ``open`` pending row past its ``expires_at`` (TTL).

    Intended to be driven once per supervisor tick. Each expired row gets
    its own ``info_event`` audit row so the expiry is in the chain.

    Args:
        session: Active async session.

    Returns:
        Number of pending rows expired by this call.
    """
    now = dt.datetime.now(dt.UTC)
    stale = (
        await session.execute(
            select(PendingApproval).where(
                PendingApproval.status == PendingStatus.open,
                PendingApproval.expires_at <= now,
            )
        )
    ).scalars().all()
    if not stale:
        return 0

    stale_ids = [p.id for p in stale]
    await session.execute(
        update(PendingApproval)
        .where(PendingApproval.id.in_(stale_ids))
        .values(status=PendingStatus.expired, decided_at=now)
    )
    await session.flush()

    for pending in stale:
        await log_audit(
            session,
            event_type=AuditEventType.info_event,
            actor_id=_SUPERVISOR_ACTOR,
            room_id=pending.room_id,
            summary=(
                f"SFW pending approval #{pending.id} expired "
                f"(TTL reached, no human decision)"
            ),
            params={
                "pending_id": pending.id,
                "decision": "expired",
                "expires_at": pending.expires_at.isoformat(),
            },
            snapshot_id=pending.snapshot_id,
            llm_call_id=pending.llm_call_id,
        )

    log.info("sfw_pending_expired", count=len(stale_ids), ids=stale_ids)
    return len(stale_ids)
