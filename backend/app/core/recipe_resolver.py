"""Phase-default + day-override merge for :class:`GrowRecipe`.

The data model is two-tier:

* :class:`app.schemas.cultivation.GrowRecipe` holds an ordered list of
  contiguous phases. Each phase declares a flat
  :class:`app.schemas.cultivation.PhaseTargetSpec` map
  (``phase.targets[param_name] -> {value, tolerance?, unit?}``) plus the
  Convex-aligned ``environmental_targets`` / ``nutrients`` rangebands.
* :class:`app.models.grow_recipe_day_override.GrowRecipeDayOverride` is
  a sparse table of per-day overrides on top of those phase defaults.
  Only deviating cells are stored.

This module's hot path is *"for recipe R, day N, param X, what's the
effective target?"*. The answer is three-step:

1. Find the phase whose ``[start_day, end_day]`` contains N.
2. Read ``phase.targets[X]`` as the phase default.
3. If an override row matches ``(recipe_id, day=N, param_name=X)``,
   shadow the default with the override's
   ``(value, tolerance, unit)`` (each field falls back to the phase
   default when the override leaves it ``None``).

Side-effect free, deterministic — every function here is a pure
transform over its inputs. The merge logic is identical for
single-cell (``resolve_effective_target``) and full-grid
(``resolve_all_effective_targets``) reads, so callers can rely on the
two staying in sync.

A separate :func:`validate_recipe` runs the structural checks the
Pydantic layer does not enforce (cross-phase contiguity, override day
inside the cycle, override params declared by *some* phase) and
returns a list of :class:`app.api.config_wizard.WizardFinding` rows —
the same shape the room-equipment wizard uses, so the eventual planner
UI can render both validators with one widget.
"""

from __future__ import annotations

from collections.abc import Iterable

from app.api.config_wizard import WizardFinding
from app.models.grow_recipe_day_override import GrowRecipeDayOverride
from app.schemas.cultivation import GrowRecipeBase, GrowRecipePhase
from app.schemas.recipe_overrides import EffectiveTarget


class RecipeResolutionError(LookupError):
    """A requested ``(day, param)`` pair has no resolvable target.

    Raised by :func:`resolve_effective_target` when:

    * ``day`` falls outside ``[1, cycle_day_count]``, or
    * no phase containing ``day`` declares ``param_name`` AND there is
      no override row that does either.

    ``LookupError`` is the parent because the failure is *missing data*
    — the recipe was traversed without structural surprise, but no cell
    is defined at the requested address. FastAPI handlers can map this
    to HTTP 404.
    """


# ---------------------------------------------------------------------------
# Phase lookup
# ---------------------------------------------------------------------------


def _phase_for_day(recipe: GrowRecipeBase, day: int) -> GrowRecipePhase:
    """Return the phase whose inclusive ``[start_day, end_day]`` contains ``day``.

    Phases are pre-sorted by ``order`` and have ``start_day`` / ``end_day``
    populated by :class:`GrowRecipeBase`'s post-validate hook; this is a
    cheap linear scan (recipes have well under 100 phases in practice).

    Raises:
        RecipeResolutionError: If ``day`` is outside the recipe's
            ``[1, cycle_day_count]`` range, or if a phase boundary is
            missing (a bug — the schema validator should have populated
            both endpoints).
    """
    if day < 1 or day > recipe.cycle_day_count:
        raise RecipeResolutionError(
            f"day {day} is outside this recipe's cycle "
            f"[1, {recipe.cycle_day_count}]"
        )
    for phase in recipe.phases:
        if phase.start_day is None or phase.end_day is None:
            raise RecipeResolutionError(
                f"phase {phase.phase_name!r} has unpopulated day "
                "boundaries — validate the recipe through "
                "GrowRecipeBase before resolving targets"
            )
        if phase.start_day <= day <= phase.end_day:
            return phase
    # Reachable only if the recipe is structurally broken (cycle_day_count
    # disagrees with phase coverage). validate_recipe surfaces this; the
    # resolver still has to raise rather than silently return wrong data.
    raise RecipeResolutionError(
        f"no phase covers day {day} — recipe is not contiguous; "
        "run validate_recipe() to see the gap"
    )


# ---------------------------------------------------------------------------
# Override indexing
# ---------------------------------------------------------------------------


def _index_overrides(
    overrides: Iterable[GrowRecipeDayOverride],
) -> dict[tuple[int, str], GrowRecipeDayOverride]:
    """Fold the override list into a ``(day, param_name) -> row`` map.

    The ORM-level ``UNIQUE (recipe_id, day, param_name)`` constraint
    guarantees at most one row per cell, so the map is well-defined
    even if the caller passes overrides from multiple recipes (the
    caller is expected to pre-filter; this function does not).
    """
    return {(o.day, o.param_name): o for o in overrides}


# ---------------------------------------------------------------------------
# Single-cell resolve
# ---------------------------------------------------------------------------


def resolve_effective_target(
    recipe: GrowRecipeBase,
    day: int,
    param: str,
    overrides: Iterable[GrowRecipeDayOverride],
) -> EffectiveTarget:
    """Resolve the effective target for one ``(day, param)`` cell.

    Looks up the phase containing ``day``, reads ``phase.targets[param]``
    as the default, then shadows each of ``value`` / ``tolerance`` /
    ``unit`` with the matching override row's field when that field is
    set. An override whose ``tolerance`` or ``unit`` is ``None`` keeps
    the phase default for those fields — partial overrides are useful
    when a grower wants to bump only the value.

    Args:
        recipe: The validated recipe (phases must have ``start_day`` /
            ``end_day`` populated — the schema does this on validate).
        day: 1-based day within the cycle.
        param: Parameter name to resolve.
        overrides: Iterable of override rows for this recipe. May
            include rows that don't match the requested cell — they are
            ignored.

    Returns:
        The resolved :class:`EffectiveTarget`. ``source`` is
        ``"day_override"`` when an override row matched the cell;
        ``"phase_default"`` otherwise.

    Raises:
        RecipeResolutionError: If ``day`` is outside the cycle, or no
            phase declares ``param`` AND no override row provides it.
    """
    phase = _phase_for_day(recipe, day)
    override = _index_overrides(overrides).get((day, param))
    default = (phase.targets or {}).get(param)

    if default is None and override is None:
        raise RecipeResolutionError(
            f"day {day} param {param!r}: no phase default and no "
            "override row defines this cell"
        )

    if override is None:
        # Pure phase default — the `default is None` branch is already
        # handled above, so it's safe to dereference here.
        assert default is not None
        return EffectiveTarget(
            day=day,
            param_name=param,
            value=default.value,
            tolerance=default.tolerance,
            unit=default.unit,
            phase_name=phase.phase_name,
            phase_order=phase.order,
            source="phase_default",
        )

    # Day override — overlay each (value, tolerance, unit) field on top
    # of the phase default. Override-side `None` for tolerance/unit
    # falls back to the default; value is always taken from the
    # override (the row itself wouldn't exist if value matched the
    # default).
    return EffectiveTarget(
        day=day,
        param_name=param,
        value=override.value,
        tolerance=(
            override.tolerance
            if override.tolerance is not None
            else (default.tolerance if default is not None else None)
        ),
        unit=(
            override.unit
            if override.unit is not None
            else (default.unit if default is not None else None)
        ),
        phase_name=phase.phase_name,
        phase_order=phase.order,
        source="day_override",
    )


# ---------------------------------------------------------------------------
# Dense grid resolve
# ---------------------------------------------------------------------------


def resolve_all_effective_targets(
    recipe: GrowRecipeBase,
    overrides: Iterable[GrowRecipeDayOverride],
) -> list[EffectiveTarget]:
    """Resolve every cell the recipe + overrides declare into a dense list.

    Walks day 1..``cycle_day_count`` and, for each day, emits one
    :class:`EffectiveTarget` for every parameter declared either by the
    phase containing that day OR by an override row that lands on that
    day. The result is the same shape the planner UI's day-grid expects:
    one cell per ``(day, param)`` pair, with ``source`` telling the UI
    whether to render a shadow style or not.

    Output order is stable: day-ascending, then param-name-ascending.
    Inside one day, all phase-default params come from the phase's
    ``targets`` map; any override-only params (rare — usually a row only
    overrides a param the phase already declares) are appended in
    sorted order so duplicate-counting is straightforward.

    Args:
        recipe: The validated recipe.
        overrides: Iterable of override rows. Rows whose ``day`` falls
            outside the cycle are dropped — :func:`validate_recipe`
            flags them as findings; the resolver itself is lenient.

    Returns:
        A list of :class:`EffectiveTarget`, one per declared cell.
    """
    indexed = _index_overrides(overrides)
    out: list[EffectiveTarget] = []
    for day in range(1, recipe.cycle_day_count + 1):
        phase = _phase_for_day(recipe, day)
        phase_params = set((phase.targets or {}).keys())
        # Overrides that land on this day for params NOT in the phase
        # targets — still emit them so the grid is complete.
        override_only_params = {
            param
            for (oday, param) in indexed
            if oday == day and param not in phase_params
        }
        for param in sorted(phase_params | override_only_params):
            try:
                out.append(
                    resolve_effective_target(recipe, day, param, indexed.values())
                )
            except RecipeResolutionError:
                # An override-only param with no phase default and the
                # override row missing (impossible here — we just
                # asserted membership) — skip rather than blow up the
                # whole grid.
                continue
    return out


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------


def _contiguity_findings(recipe: GrowRecipeBase) -> list[WizardFinding]:
    """Hard-refusal findings for the phase contiguity / coverage rules.

    The Pydantic layer already sorted phases by ``order`` and stamped
    ``start_day`` / ``end_day`` via prefix-sum, so by construction
    consecutive phases CAN'T overlap or have a gap *within the
    derived shape*. What CAN go wrong:

    * No phases at all — :class:`GrowRecipeBase` already rejects this
      with ``min_length=1``; defensive check kept here as a belt-and-
      braces finding.
    * First phase doesn't start at day 1 — only reachable if a caller
      hand-mutated ``start_day`` after the validator ran; defensive.
    * Internal gap / overlap — same: only reachable on hand-mutated
      boundaries.

    Each finding is surfaced as a separate :class:`WizardFinding` so the
    planner UI can render them individually rather than as one giant
    blob.
    """
    findings: list[WizardFinding] = []
    if not recipe.phases:
        findings.append(
            WizardFinding(
                code="recipe_no_phases",
                hard=True,
                message=(
                    "Recipe has no phases — at least one phase is required "
                    "so every day in the cycle is covered."
                ),
            )
        )
        return findings

    first = recipe.phases[0]
    if first.start_day != 1:
        findings.append(
            WizardFinding(
                code="recipe_phases_do_not_start_at_day_1",
                hard=True,
                message=(
                    f"First phase {first.phase_name!r} starts at day "
                    f"{first.start_day}, not day 1 — phases must cover "
                    "the cycle from day 1."
                ),
            )
        )

    prev = first
    for phase in recipe.phases[1:]:
        if (
            phase.start_day is None
            or prev.end_day is None
            or phase.start_day != prev.end_day + 1
        ):
            findings.append(
                WizardFinding(
                    code="recipe_phases_not_contiguous",
                    hard=True,
                    message=(
                        f"Phase {prev.phase_name!r} ends at day "
                        f"{prev.end_day} but next phase "
                        f"{phase.phase_name!r} starts at day "
                        f"{phase.start_day} — phases must be contiguous "
                        "with no gap or overlap."
                    ),
                )
            )
        prev = phase

    last = recipe.phases[-1]
    if last.end_day != recipe.cycle_day_count:
        findings.append(
            WizardFinding(
                code="recipe_phases_do_not_cover_cycle",
                hard=True,
                message=(
                    f"Last phase {last.phase_name!r} ends at day "
                    f"{last.end_day} but the cycle is "
                    f"{recipe.cycle_day_count} days — phases must cover "
                    "the whole cycle."
                ),
            )
        )

    return findings


def _override_findings(
    recipe: GrowRecipeBase,
    overrides: Iterable[GrowRecipeDayOverride],
) -> list[WizardFinding]:
    """Findings for the override list against the validated recipe.

    Hard refusals:

    * Override whose ``day`` is outside ``[1, cycle_day_count]``.

    Fail-soft warnings:

    * Override whose ``param_name`` is declared by NO phase's targets —
      an "orphan" override that the UI can still render, but the
      grower probably intended a typo correction.
    """
    findings: list[WizardFinding] = []
    declared_params: set[str] = set()
    for phase in recipe.phases:
        if phase.targets:
            declared_params.update(phase.targets.keys())

    for override in overrides:
        if override.day < 1 or override.day > recipe.cycle_day_count:
            findings.append(
                WizardFinding(
                    code="override_day_out_of_cycle",
                    hard=True,
                    message=(
                        f"Override on day {override.day} param "
                        f"{override.param_name!r} is outside the recipe's "
                        f"cycle [1, {recipe.cycle_day_count}]."
                    ),
                )
            )
            continue
        if declared_params and override.param_name not in declared_params:
            findings.append(
                WizardFinding(
                    code="override_param_orphan",
                    hard=False,
                    message=(
                        f"Override on day {override.day} param "
                        f"{override.param_name!r}: no phase declares this "
                        "parameter in its targets map — likely a typo."
                    ),
                )
            )
    return findings


def _empty_phase_findings(recipe: GrowRecipeBase) -> list[WizardFinding]:
    """Warning for any phase whose ``targets`` map is missing or empty.

    Such a phase will have NO effective targets unless every single day
    in it is shadowed by an override row. Recoverable (the grower may
    actually intend full per-day control inside that phase) so it's a
    warning, not a refusal.
    """
    findings: list[WizardFinding] = []
    for phase in recipe.phases:
        if not phase.targets:
            findings.append(
                WizardFinding(
                    code="phase_targets_empty",
                    hard=False,
                    message=(
                        f"Phase {phase.phase_name!r} (days "
                        f"{phase.start_day}..{phase.end_day}) has no "
                        "targets — every day in this phase will need an "
                        "override row to produce an effective target."
                    ),
                )
            )
    return findings


def validate_recipe(
    recipe: GrowRecipeBase,
    overrides: Iterable[GrowRecipeDayOverride],
) -> list[WizardFinding]:
    """Run every structural check over a recipe + its overrides.

    Returns one flat list of :class:`WizardFinding` rows; callers split
    by ``finding.hard`` to render hard refusals separately from
    warnings (same convention as
    :mod:`app.api.config_wizard.validate_room_config`).

    The Pydantic validator on :class:`GrowRecipeBase` already enforced
    well-formed structure (one or more phases, unique ``order``,
    integer ``duration_days``, populated ``start_day`` / ``end_day``);
    this function adds the *semantic* checks the schema layer can't:

    * Cross-phase contiguity / coverage — first phase at day 1, last
      phase at ``cycle_day_count``, no gaps or overlaps.
    * Each override's ``day`` inside the cycle.
    * Orphan overrides — a warning when a param isn't declared by any
      phase.
    * Empty phase target maps — a warning when a phase has no
      defaults.

    Args:
        recipe: The validated recipe (Pydantic post-validate has run).
        overrides: Iterable of override rows for this recipe.

    Returns:
        Every :class:`WizardFinding` that fired, in the order
        (contiguity, override, empty-phase). Empty when the recipe and
        overrides are clean.
    """
    # Materialise overrides to a tuple so we can scan twice (once for
    # override findings, once for phase findings) without re-iterating
    # a one-shot iterator.
    override_list = tuple(overrides)

    findings: list[WizardFinding] = []
    findings.extend(_contiguity_findings(recipe))
    findings.extend(_override_findings(recipe, override_list))
    findings.extend(_empty_phase_findings(recipe))
    return findings
