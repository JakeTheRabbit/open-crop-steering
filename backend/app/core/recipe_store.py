"""Recipe revision store — create + approve + read immutable recipes.

A recipe is a per-room cultivation plan: one ``recipe_revision`` header
plus one ``recipe_revision_param`` row per ``(day_index, param_name)``.

This module is the *only* sanctioned way humans mutate the recipe
catalogue, and it embodies locked decision #4 (immutable revisions):

* :func:`create_revision` always INSERTs a brand-new revision at the next
  version number for the room. It never UPDATEs an existing row.
* :func:`approve_revision` flips a ``draft`` revision to ``approved`` and
  supersedes the previously-approved revision for that room. Once a
  revision is ``approved`` it is frozen — DB triggers (baseline
  migration) reject any further mutation of the header or its params.
* The AI never calls anything here; it only adds runtime overlays via
  :mod:`app.core.overlays`.

Async-SQLAlchemy note: child ``recipe_revision_param`` rows are inserted
by direct FK assignment, and the relationship is re-fetched with
``selectinload`` when a caller needs it — accessing a lazy relationship
outside a greenlet context raises ``MissingGreenlet``.
"""

from __future__ import annotations

from typing import Any, TypedDict

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.audit import log_audit
from app.models.audit_event import AuditEventType
from app.models.recipe_revision import (
    RecipeRevision,
    RecipeRevisionParam,
    RecipeStatus,
)

log = structlog.get_logger(__name__)


class RecipeParamSpec(TypedDict, total=False):
    """Shape of one entry in the ``params`` list passed to :func:`create_revision`.

    ``day_index``, ``param_name`` and ``value`` are required; ``tolerance``
    and ``unit`` are optional.
    """

    day_index: int
    param_name: str
    value: float
    tolerance: float | None
    unit: str | None


async def _next_version(session: AsyncSession, room_id: str) -> int:
    """Return the next free version number for ``room_id`` (1-based)."""
    result = await session.execute(
        select(func.max(RecipeRevision.version)).where(
            RecipeRevision.room_id == room_id
        )
    )
    current = result.scalar_one_or_none()
    return 1 if current is None else current + 1


async def create_revision(
    session: AsyncSession,
    *,
    room_id: str,
    name: str,
    params: list[RecipeParamSpec | dict[str, Any]],
    created_by: str,
    cycle_day_count: int = 84,
    notes: str | None = None,
) -> RecipeRevision:
    """Create a NEW draft recipe revision for a room.

    Always inserts a fresh revision at the next version number for the
    room; never mutates an existing revision. The revision lands in
    ``draft`` status and must be approved by a QAP via
    :func:`approve_revision` before it becomes the active recipe.

    Args:
        session: Active async session.
        room_id: Room the recipe belongs to.
        name: Human-readable revision name.
        params: List of ``{day_index, param_name, value, tolerance?,
            unit?}`` dicts. One entry per (day, param) cell.
        created_by: ``users.id`` of the cultivator creating the revision.
        cycle_day_count: Length of the cultivation cycle in days.
        notes: Optional free-text notes.

    Returns:
        The persisted :class:`RecipeRevision` with ``params`` eagerly
        loaded.
    """
    version = await _next_version(session, room_id)

    revision = RecipeRevision(
        room_id=room_id,
        version=version,
        name=name,
        cycle_day_count=cycle_day_count,
        status=RecipeStatus.draft,
        created_by=created_by,
        notes=notes,
    )
    session.add(revision)
    await session.flush()  # assigns revision.id

    for spec in params:
        session.add(
            RecipeRevisionParam(
                recipe_revision_id=revision.id,
                room_id=room_id,
                day_index=int(spec["day_index"]),
                param_name=str(spec["param_name"]),
                value=float(spec["value"]),
                tolerance=(
                    None
                    if spec.get("tolerance") is None
                    else float(spec["tolerance"])  # type: ignore[arg-type]
                ),
                unit=spec.get("unit"),
            )
        )
    await session.flush()

    await log_audit(
        session,
        event_type=AuditEventType.recipe_revision_created,
        actor_id=created_by,
        room_id=room_id,
        summary=f"Created recipe revision v{version} '{name}' ({len(params)} params)",
        recipe_revision_id=revision.id,
        params={"version": version, "param_count": len(params)},
    )

    log.info(
        "recipe_revision_created",
        room_id=room_id,
        revision_id=revision.id,
        version=version,
        param_count=len(params),
    )

    # Re-fetch with params eagerly loaded so callers can read .params
    # without tripping the async lazy loader.
    return await _reload_with_params(session, revision.id)


async def approve_revision(
    session: AsyncSession,
    revision_id: int,
    qap_user: str,
) -> RecipeRevision:
    """Approve a draft revision and supersede the prior active one.

    Flips ``draft`` -> ``approved``, records ``approved_by`` /
    ``approved_at``, and marks the room's previously-approved revision as
    ``superseded`` so exactly one revision is active per room.

    Args:
        session: Active async session.
        revision_id: Revision to approve.
        qap_user: ``users.id`` of the QAP approving.

    Returns:
        The approved :class:`RecipeRevision` with ``params`` loaded.

    Raises:
        ValueError: If the revision does not exist or is not a draft.
    """
    revision = await session.get(RecipeRevision, revision_id)
    if revision is None:
        raise ValueError(f"recipe revision {revision_id} not found")
    if revision.status != RecipeStatus.draft:
        raise ValueError(
            f"recipe revision {revision_id} is {revision.status.value}, "
            "only draft revisions can be approved"
        )

    # Supersede the currently-approved revision for this room (if any).
    prior_result = await session.execute(
        select(RecipeRevision).where(
            RecipeRevision.room_id == revision.room_id,
            RecipeRevision.status == RecipeStatus.approved,
            RecipeRevision.id != revision.id,
        )
    )
    superseded_ids: list[int] = []
    for prior in prior_result.scalars():
        prior.status = RecipeStatus.superseded
        superseded_ids.append(prior.id)

    revision.status = RecipeStatus.approved
    revision.approved_by = qap_user
    revision.approved_at = func.now()
    await session.flush()

    await log_audit(
        session,
        event_type=AuditEventType.recipe_revision_approved,
        actor_id=qap_user,
        room_id=revision.room_id,
        summary=(
            f"Approved recipe revision v{revision.version}"
            + (f"; superseded {superseded_ids}" if superseded_ids else "")
        ),
        recipe_revision_id=revision.id,
        params={"version": revision.version, "superseded": superseded_ids},
    )

    log.info(
        "recipe_revision_approved",
        room_id=revision.room_id,
        revision_id=revision.id,
        version=revision.version,
        superseded=superseded_ids,
    )

    return await _reload_with_params(session, revision.id)


async def get_active_revision(
    session: AsyncSession,
    room_id: str,
) -> RecipeRevision | None:
    """Return the highest-version approved revision for a room.

    Args:
        session: Active async session.
        room_id: Room to look up.

    Returns:
        The active :class:`RecipeRevision` with ``params`` eagerly
        loaded, or ``None`` if the room has no approved revision.
    """
    result = await session.execute(
        select(RecipeRevision)
        .options(selectinload(RecipeRevision.params))
        .where(
            RecipeRevision.room_id == room_id,
            RecipeRevision.status == RecipeStatus.approved,
        )
        .order_by(RecipeRevision.version.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def self_heal(
    session: AsyncSession,
    room_id: str,
) -> RecipeRevision | None:
    """Recover from a corrupt active revision (plan risk #13).

    If the room's active (highest-version approved) revision has zero
    params it is treated as corrupt; this falls back to the most recent
    *previously-approved* revision that still has params. Because
    approving a new revision supersedes the old one, the fallback search
    spans both ``approved`` and ``superseded`` revisions (a superseded
    revision is one that was approved at some point). The corrupt
    revision is left untouched — it is immutable — and the caller is
    expected to raise a ``critical_incident`` and have a human cut a
    fresh revision.

    Args:
        session: Active async session.
        room_id: Room to heal.

    Returns:
        A healthy :class:`RecipeRevision` (params loaded): the active
        revision if it is fine, otherwise the newest prior approved /
        superseded revision that has params. ``None`` if no
        ever-approved revision for the room has any params.
    """
    # Param counts are read with a direct SQL aggregate rather than the
    # ORM ``.params`` collection so a possibly-stale identity-mapped
    # revision in this session cannot mask a corrupt (param-less) row.
    rows = (
        await session.execute(
            select(
                RecipeRevision.id,
                RecipeRevision.version,
                func.count(RecipeRevisionParam.id),
            )
            .outerjoin(
                RecipeRevisionParam,
                RecipeRevisionParam.recipe_revision_id == RecipeRevision.id,
            )
            .where(
                RecipeRevision.room_id == room_id,
                RecipeRevision.status.in_(
                    (RecipeStatus.approved, RecipeStatus.superseded)
                ),
            )
            .group_by(RecipeRevision.id, RecipeRevision.version)
            .order_by(RecipeRevision.version.desc())
        )
    ).all()
    if not rows:
        log.warning("self_heal_no_approved_revision", room_id=room_id)
        return None

    active_id, active_version, active_param_count = rows[0]
    if active_param_count > 0:
        # Active revision is healthy — nothing to heal.
        return await _reload_with_params(session, active_id)

    for fallback_id, fallback_version, fallback_param_count in rows[1:]:
        if fallback_param_count > 0:
            log.warning(
                "recipe_self_heal",
                room_id=room_id,
                corrupt_revision_id=active_id,
                fallback_revision_id=fallback_id,
            )
            await log_audit(
                session,
                event_type=AuditEventType.critical_incident,
                actor_id=None,
                room_id=room_id,
                summary=(
                    f"Recipe self-heal: active revision v{active_version} "
                    f"has no params; fell back to v{fallback_version}"
                ),
                recipe_revision_id=fallback_id,
                reason_codes=["recipe_self_heal"],
                params={
                    "corrupt_revision_id": active_id,
                    "fallback_revision_id": fallback_id,
                },
            )
            return await _reload_with_params(session, fallback_id)

    log.error("self_heal_all_revisions_empty", room_id=room_id)
    return None


async def _reload_with_params(
    session: AsyncSession, revision_id: int
) -> RecipeRevision:
    """Re-fetch a revision with ``params`` eagerly loaded."""
    result = await session.execute(
        select(RecipeRevision)
        .options(selectinload(RecipeRevision.params))
        .where(RecipeRevision.id == revision_id)
    )
    return result.scalar_one()
