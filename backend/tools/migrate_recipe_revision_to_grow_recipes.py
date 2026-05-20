"""One-shot migration: ``recipe_revision`` -> ``grow_recipes`` + day overrides.

Pass 6 of the OCS↔AiGrowApp schema alignment. The legacy
``recipe_revision`` / ``recipe_revision_param`` tables hold one row per
``(revision, day_index, param_name)`` — the per-day setpoint flow that
the supervisor reads from today via the ``effective_target`` materialized
view. The new ``grow_recipes`` table (pass 4) stores AiGrowApp's
phase-based shape, and ``grow_recipe_day_overrides`` (pass 6) holds the
sparse per-day overrides on top.

This script does a **lossless** one-to-one move from the legacy shape to
the new shape:

* Each ``RecipeRevision`` becomes ONE ``grow_recipes`` row.
* The new recipe has ONE big phase covering all days
  (``start_day=1, end_day=cycle_day_count``, empty ``targets`` map).
* Every existing ``RecipeRevisionParam`` row becomes a
  ``grow_recipe_day_overrides`` row.

The user can later refactor each migrated recipe into multiple
meaningful phases by editing in the planner UI — but until they do, the
migrated state is equivalent to the legacy one: every cell is an
explicit override of a single phase that declares no defaults of its
own. The resolver handles that case by returning the override as the
sole source of truth for the cell (the single-phase default of "nothing"
gets shadowed by every override row).

USAGE (NOT auto-run — review first, then run manually):

    cd backend
    ../.venv/Scripts/python -m tools.migrate_recipe_revision_to_grow_recipes

The script:

1. Connects to ``DATABASE_URL`` (same env var the app uses).
2. Streams every ``RecipeRevision`` in the database.
3. For each, computes a fresh ``grow_recipes`` row + per-day override
   rows via :func:`convert_revision`.
4. Inserts both inside a single transaction per revision (per-revision
   isolation makes the partial-failure story trivial: a poison revision
   skips with a logged error rather than blowing the batch).

The script writes audit events to stdout, not the ``audit_event`` chain
— audit chain inserts require the per-session HMAC GUC and a real user
id, neither of which a one-shot tool should fake. The migration is
small enough that the operator can spot-check the inserted rows
manually.

The per-revision conversion logic is isolated in
:func:`convert_revision` (no I/O) so it is unit-testable with
hand-crafted shapes — see ``tests/unit/test_recipe_resolver.py``.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import uuid

import structlog
from app.config import get_settings
from app.db import dispose_engine, get_session_factory
from app.models.grow_recipe import GrowRecipe
from app.models.grow_recipe_day_override import GrowRecipeDayOverride
from app.models.recipe_revision import RecipeRevision
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

log = structlog.get_logger(__name__)


@dataclasses.dataclass(frozen=True)
class ConversionResult:
    """The (one recipe, N overrides) tuple a single revision converts into.

    Returned by :func:`convert_revision` so callers can decide how to
    persist (or, in unit tests, just inspect) the produced rows. All
    fields are detached SQLAlchemy instances — they are not bound to
    any session.
    """

    recipe: GrowRecipe
    overrides: list[GrowRecipeDayOverride]


def _placeholder_phase(cycle_day_count: int) -> dict[str, object]:
    """Build the single placeholder phase covering ``[1, cycle_day_count]``.

    The phase has NO ``targets`` — every cell of effective targets comes
    from overrides. ``start_day`` / ``end_day`` are recorded on the
    phase dict alongside the Convex-aligned ``durationDays`` / ``order``
    so the planner UI can render boundaries without re-validating the
    recipe; the schema's post-validate hook will also (re-)stamp them
    on read, so the two stay consistent.
    """
    return {
        "phaseName": "Imported recipe (single phase)",
        "durationDays": float(cycle_day_count),
        "order": 1.0,
        "startDay": 1,
        "endDay": cycle_day_count,
    }


def convert_revision(
    revision: RecipeRevision,
    *,
    org_id: str,
    now: dt.datetime | None = None,
) -> ConversionResult:
    """Convert one legacy revision into ``grow_recipes`` + day-override rows.

    Side-effect free: returns detached SQLAlchemy instances; the caller
    is responsible for ``session.add`` / commit. ``now`` is supplied as
    a parameter so unit tests can pin timestamps without freezegun.

    Args:
        revision: A :class:`RecipeRevision` with ``params`` loaded
            (use ``selectinload(RecipeRevision.params)`` on the SELECT).
        org_id: The tenant id to stamp on every new row.
        now: Optional timestamp override for ``created_at`` /
            ``updated_at``. Defaults to the current UTC time.

    Returns:
        A :class:`ConversionResult` whose ``recipe`` is a fresh
        :class:`GrowRecipe` and whose ``overrides`` is the per-day
        override list (one entry per ``RecipeRevisionParam``).
    """
    if now is None:
        now = dt.datetime.now(dt.UTC)

    recipe_id = str(uuid.uuid4())
    recipe = GrowRecipe(
        id=recipe_id,
        org_id=org_id,
        name=(
            f"{revision.name} (room {revision.room_id} v{revision.version})"
        ),
        recipe_type=["imported_legacy"],
        description=(
            "Imported from legacy recipe_revision "
            f"#{revision.id} (room {revision.room_id} v{revision.version}). "
            "Single placeholder phase covering the whole cycle; every "
            "per-day setpoint is an override row. The grower can refactor "
            "this into meaningful phases in the planner UI."
        ),
        version=float(revision.version),
        is_active=True,
        estimated_total_duration_days=float(revision.cycle_day_count),
        phases=[_placeholder_phase(revision.cycle_day_count)],
        created_by=revision.created_by,
        last_modified_by=revision.approved_by or revision.created_by,
        created_at=now,
        updated_at=now,
    )

    overrides: list[GrowRecipeDayOverride] = []
    for param in revision.params:
        overrides.append(
            GrowRecipeDayOverride(
                id=str(uuid.uuid4()),
                org_id=org_id,
                recipe_id=recipe_id,
                day=int(param.day_index),
                param_name=str(param.param_name),
                value=float(param.value),
                tolerance=(
                    None if param.tolerance is None else float(param.tolerance)
                ),
                unit=param.unit,
                created_at=now,
                updated_at=now,
            )
        )

    return ConversionResult(recipe=recipe, overrides=overrides)


async def _persist_one(
    session: AsyncSession, result: ConversionResult
) -> None:
    """Add the recipe + every override to ``session`` and flush.

    Per-revision flush (the caller commits) keeps the unique-key check
    on overrides local to one revision and surfaces violations
    immediately with the offending row in scope.
    """
    session.add(result.recipe)
    for override in result.overrides:
        session.add(override)
    await session.flush()


async def migrate_all() -> None:
    """Stream every :class:`RecipeRevision` and persist its converted shape.

    Per-revision transaction:

    * Begins via the per-revision session boundary.
    * On success: commit, log success.
    * On failure: log error with the revision id, roll back, continue
      to the next revision. A poison revision does not block the rest.

    This is intentionally NOT idempotent in the cheapest sense (no
    ``ON CONFLICT``): each run inserts a fresh ``grow_recipes`` row with
    a new UUID, even if a prior run already imported the same revision.
    The operator is expected to run this once after a fresh
    ``grow_recipe_day_overrides`` migration; running it twice produces
    duplicate ``grow_recipes`` rows whose ``description`` (the source
    revision id is embedded) makes the duplicate trivial to spot.
    """
    settings = get_settings()
    factory = get_session_factory()

    succeeded = 0
    failed = 0

    async with factory() as session:
        result = await session.execute(
            select(RecipeRevision).options(
                selectinload(RecipeRevision.params)
            )
        )
        revisions = list(result.scalars())
        log.info("migrate_revisions_loaded", count=len(revisions))

        for revision in revisions:
            try:
                converted = convert_revision(
                    revision, org_id=settings.ocs_org_id
                )
                await _persist_one(session, converted)
                await session.commit()
                log.info(
                    "migrate_revision_ok",
                    revision_id=revision.id,
                    room_id=revision.room_id,
                    version=revision.version,
                    grow_recipe_id=converted.recipe.id,
                    overrides=len(converted.overrides),
                )
                succeeded += 1
            except Exception:
                await session.rollback()
                log.exception(
                    "migrate_revision_failed",
                    revision_id=revision.id,
                    room_id=revision.room_id,
                    version=revision.version,
                )
                failed += 1

    await dispose_engine()
    log.info(
        "migrate_complete", succeeded=succeeded, failed=failed
    )


if __name__ == "__main__":
    asyncio.run(migrate_all())
