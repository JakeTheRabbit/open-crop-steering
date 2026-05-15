"""Deterministic ``allowed_action_set`` generator.

Before the LLM is consulted, a deterministic engine pre-computes the
"boring-safe" menu of options for the snapshot — for each setpoint the
room is steering, a *small step up*, a *small step down*, and *no
change*. This is the :func:`build_action_set` output.

Why a deterministic menu (plan locked decision #13):

* It bounds what a well-behaved model needs to choose between — the LLM
  picks an ``action_id`` from the set.
* The model may *also* return a free-form ``proposed_changes`` list; the
  Phase 9 validator decides whether a free-form proposal is in-set,
  in-bounds-but-novel, or out-of-bounds. Phase 7 only *builds* the set
  and runs report mode — it never applies anything.
* A "no change" option always exists so "leave it alone" is a
  first-class, citable choice rather than the absence of one.

Each :class:`Action` has a stable, content-derived ``action_id`` so the
same snapshot always yields the same menu (idempotent — important for
the snapshot/echo contract).

Step sizes are derived from the parameter's tolerance band where the
snapshot carries one, falling back to a per-parameter default. A step is
deliberately *small* — one tolerance band — so even the boldest in-set
choice is a gentle nudge, never a leap.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

import structlog

from app.core.rollout import ParamClass, class_for_param

log = structlog.get_logger(__name__)

#: Per-parameter default step size, used when the snapshot setpoint has
#: no tolerance band attached. Conservative — one "small nudge" worth.
_DEFAULT_STEP: dict[str, float] = {
    "temp": 0.2,
    "temp_day": 0.2,
    "temp_night": 0.2,
    "rh": 1.0,
    "rh_day": 1.0,
    "rh_night": 1.0,
    "co2": 25.0,
    "co2_day": 25.0,
    "vpd_day": 0.05,
    "ppfd": 20.0,
    "vwc_target": 1.0,
    "ec_target": 0.1,
    "dryback_pct": 1.0,
}

#: Fallback step when a parameter is in neither table.
_GENERIC_STEP = 1.0

#: How the three options per parameter are labelled.
DIRECTION_UP = "increase"
DIRECTION_DOWN = "decrease"
DIRECTION_HOLD = "no_change"


@dataclass(slots=True)
class Action:
    """One boring-safe option in the allowed action set.

    Attributes:
        action_id: Stable, content-derived identifier. The LLM echoes one
            of these as ``recommended_action_id``.
        param_name: The setpoint the action concerns.
        param_class: The parameter's risk class (A-E).
        direction: ``increase`` / ``decrease`` / ``no_change``.
        delta: Signed change the action would apply (``0.0`` for hold).
        current_value: The setpoint's value in the snapshot (may be
            ``None`` if the snapshot did not carry it).
        resulting_value: ``current_value + delta`` when both are known,
            else ``None``.
        description: Human-readable summary for the LLM prompt + UI.
    """

    action_id: str
    param_name: str
    param_class: ParamClass
    direction: str
    delta: float
    current_value: float | None
    resulting_value: float | None
    description: str

    def to_dict(self) -> dict[str, Any]:
        """Serialise the action for the LLM prompt / API payloads."""
        return {
            "action_id": self.action_id,
            "param_name": self.param_name,
            "param_class": self.param_class.value,
            "direction": self.direction,
            "delta": self.delta,
            "current_value": self.current_value,
            "resulting_value": self.resulting_value,
            "description": self.description,
        }


@dataclass(slots=True)
class ParamCandidate:
    """A parameter the action set should offer options for.

    Attributes:
        param_name: The setpoint name.
        current_value: Its current value from the snapshot, if known.
        tolerance: Its tolerance band, if the snapshot carried one — used
            as the step size in preference to the per-parameter default.
    """

    param_name: str
    current_value: float | None = None
    tolerance: float | None = None


def _step_for(candidate: ParamCandidate) -> float:
    """Pick the step size for a candidate — its tolerance band, or a default."""
    if candidate.tolerance is not None and candidate.tolerance > 0:
        return abs(candidate.tolerance)
    return _DEFAULT_STEP.get(candidate.param_name, _GENERIC_STEP)


def _action_id(param_name: str, direction: str, delta: float) -> str:
    """Build a stable, content-derived action id.

    The id is ``act-{param}-{direction}-{hash}`` where the hash covers
    the param, direction and delta — so an identical option always gets
    an identical id (idempotent menus) while distinct deltas differ.
    """
    digest = hashlib.sha256(
        f"{param_name}|{direction}|{delta:.6f}".encode()
    ).hexdigest()[:8]
    return f"act-{param_name}-{direction}-{digest}"


def _make_action(
    candidate: ParamCandidate, direction: str, delta: float
) -> Action:
    """Build one :class:`Action` for a candidate parameter + direction."""
    param_class = class_for_param(candidate.param_name)
    current = candidate.current_value
    resulting = None if current is None else round(current + delta, 6)

    if direction == DIRECTION_HOLD:
        description = f"Hold {candidate.param_name} at its current setpoint"
    else:
        verb = "Raise" if delta > 0 else "Lower"
        description = (
            f"{verb} {candidate.param_name} by {abs(delta):g} "
            f"({'+' if delta > 0 else ''}{delta:g})"
        )

    return Action(
        action_id=_action_id(candidate.param_name, direction, delta),
        param_name=candidate.param_name,
        param_class=param_class,
        direction=direction,
        delta=delta,
        current_value=current,
        resulting_value=resulting,
        description=description,
    )


def _candidates_from_snapshot(snapshot: Any) -> list[ParamCandidate]:
    """Derive the parameter candidates from a snapshot's payload.

    Reads ``payload["setpoints"]`` (the room's steered setpoints) and,
    where the recipe metadata is present, their tolerance bands. A
    setpoint whose value is ``None`` (not currently known) is skipped —
    there is nothing safe to step from.

    Args:
        snapshot: A :class:`~app.models.sensor_snapshot.SensorSnapshot`
            (or any object exposing a ``payload`` dict).

    Returns:
        One :class:`ParamCandidate` per usable setpoint.
    """
    payload: dict[str, Any] = getattr(snapshot, "payload", {}) or {}
    setpoints: dict[str, Any] = payload.get("setpoints", {}) or {}
    tolerances: dict[str, Any] = payload.get("tolerances", {}) or {}

    candidates: list[ParamCandidate] = []
    for param_name, value in setpoints.items():
        if value is None:
            continue
        tol = tolerances.get(param_name)
        candidates.append(
            ParamCandidate(
                param_name=param_name,
                current_value=float(value),
                tolerance=None if tol is None else float(tol),
            )
        )
    return candidates


def build_action_set(
    snapshot: Any,
    *,
    candidates: list[ParamCandidate] | None = None,
) -> list[Action]:
    """Build the boring-safe ``allowed_action_set`` for a snapshot.

    For every steered setpoint the room has (derived from the snapshot
    payload, or supplied explicitly via ``candidates``) three options are
    emitted: a small step up, a small step down, and no change. The step
    size is the parameter's tolerance band, falling back to a
    conservative per-parameter default.

    The result is deterministic: the same snapshot always yields the same
    list, with stable ``action_id`` values — which the snapshot/echo
    contract relies on.

    Args:
        snapshot: The :class:`~app.models.sensor_snapshot.SensorSnapshot`
            the menu is for. Used only to read its ``payload`` when
            ``candidates`` is not supplied.
        candidates: Explicit parameter candidates, overriding payload
            derivation (useful for callers that already know the steered
            params + tolerances).

    Returns:
        The list of :class:`Action` options. Always includes at least the
        per-parameter ``no_change`` action for every candidate; empty
        only if there are no candidates at all.
    """
    resolved = (
        candidates
        if candidates is not None
        else _candidates_from_snapshot(snapshot)
    )

    actions: list[Action] = []
    for candidate in resolved:
        step = _step_for(candidate)
        actions.append(_make_action(candidate, DIRECTION_UP, step))
        actions.append(_make_action(candidate, DIRECTION_DOWN, -step))
        actions.append(_make_action(candidate, DIRECTION_HOLD, 0.0))

    log.debug(
        "action_set_built",
        snapshot_id=getattr(snapshot, "id", None),
        candidates=len(resolved),
        actions=len(actions),
    )
    return actions


def action_set_to_payload(actions: list[Action]) -> list[dict[str, Any]]:
    """Serialise an action set for the LLM prompt / ``llm_call_log``."""
    return [a.to_dict() for a in actions]
