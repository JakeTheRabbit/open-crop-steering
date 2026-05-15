"""In-band tolerance checks for cultivation setpoints.

A recipe parameter carries an effective *target* and a *tolerance* band
(``{v: 28, tol: 0.5}`` in the plan's notation). This module turns a
measured sensor value into a verdict — is the room actually tracking the
recipe, or has it drifted enough to be worth escalating?

The split that matters for the rest of Phase 6+:

* **in_band** — silent. The supervisor does not call the LLM, no event
  is logged. Tolerance bands are what keep the AI quiet inside normal
  operation (plan risk #12).
* **minor drift** — out of band but within ``2 x tolerance``. Worth a
  ``system_warning`` / digest entry, not an incident.
* **major drift** — beyond ``2 x tolerance``. The room is materially off
  target; the state machine treats it as an IMPAIRED-grade tolerance
  breach.

Everything here is pure (no DB, no Influx) except
:func:`check_room_tolerances`, which joins effective targets against live
sensor readings via the injected :class:`~app.influx_client.InfluxClient`.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

import structlog
from sqlalchemy import text

from app.core.effective import effective_targets_for_day

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger(__name__)

#: Name of the effective-target materialized view (mirrors
#: :mod:`app.core.effective`).
_EFFECTIVE_MATVIEW = "effective_target"

#: Multiple of the tolerance band beyond which a drift is "major" rather
#: than "minor". A value 1 tol out of band is minor; >2 tol is major.
_MAJOR_DRIFT_FACTOR = 2.0

#: Fallback tolerance used when an effective target has no ``tolerance``
#: set. Deliberately tiny so a missing band fails toward "escalate"
#: rather than silently swallowing drift.
_DEFAULT_TOLERANCE = 0.0


class DriftClass(enum.StrEnum):
    """How far a measured value sits from its target band."""

    in_band = "in_band"
    minor = "minor"
    major = "major"


class _SensorReader(Protocol):
    """Structural type for the bit of ``InfluxClient`` used here."""

    def current_value(self, entity: str) -> float | None:  # pragma: no cover
        ...


@dataclass(slots=True)
class DriftResult:
    """Verdict for a single ``actual`` vs ``target`` comparison.

    Attributes:
        drift: The :class:`DriftClass` bucket.
        actual: The measured value compared.
        target: The effective target compared against.
        tolerance: The tolerance band used (>= 0).
        deviation: Signed ``actual - target``.
        exceedance: How far *outside* the band the value sits, in the
            same units (0.0 when in band).
    """

    drift: DriftClass
    actual: float
    target: float
    tolerance: float
    deviation: float
    exceedance: float

    @property
    def in_band(self) -> bool:
        """``True`` iff the value is inside its tolerance band."""
        return self.drift is DriftClass.in_band


@dataclass(slots=True)
class ToleranceBreach:
    """An out-of-band parameter found by :func:`check_room_tolerances`.

    Only *minor* and *major* drifts produce a breach; in-band parameters
    are not reported (they are the silent-success case).

    Attributes:
        room_id: Room the breach belongs to.
        param_name: Recipe parameter that drifted.
        day_index: Cycle day the effective target was read for.
        result: The full :class:`DriftResult`.
    """

    room_id: str
    param_name: str
    day_index: int
    result: DriftResult

    @property
    def is_major(self) -> bool:
        """``True`` iff the underlying drift is :attr:`DriftClass.major`."""
        return self.result.drift is DriftClass.major


def in_band(actual: float, target: float, tolerance: float) -> bool:
    """Return whether ``actual`` is within ``tolerance`` of ``target``.

    The band is inclusive on both edges: a value exactly ``tolerance``
    away from ``target`` counts as in band.

    Args:
        actual: Measured value.
        target: Effective target value.
        tolerance: Half-width of the acceptable band. Negative values are
            treated as their absolute value.

    Returns:
        ``True`` if ``abs(actual - target) <= abs(tolerance)``.
    """
    return abs(actual - target) <= abs(tolerance)


def classify_drift(
    actual: float, target: float, tolerance: float
) -> DriftResult:
    """Bucket an ``actual`` vs ``target`` comparison into a drift class.

    Boundaries (with ``tol = abs(tolerance)``):

    * ``|actual - target| <= tol`` -> :attr:`DriftClass.in_band`
    * ``tol < |actual - target| <= 2 * tol`` -> :attr:`DriftClass.minor`
    * ``|actual - target| > 2 * tol`` -> :attr:`DriftClass.major`

    When ``tol`` is ``0`` the band collapses: an exact match is in band,
    anything else is major (there is no minor zone to fall into).

    Args:
        actual: Measured value.
        target: Effective target value.
        tolerance: Half-width of the acceptable band.

    Returns:
        A :class:`DriftResult` describing the comparison.
    """
    tol = abs(tolerance)
    deviation = actual - target
    distance = abs(deviation)

    if distance <= tol:
        drift = DriftClass.in_band
        exceedance = 0.0
    elif distance <= _MAJOR_DRIFT_FACTOR * tol:
        drift = DriftClass.minor
        exceedance = distance - tol
    else:
        drift = DriftClass.major
        exceedance = distance - tol

    return DriftResult(
        drift=drift,
        actual=actual,
        target=target,
        tolerance=tol,
        deviation=deviation,
        exceedance=exceedance,
    )


async def _effective_targets_populated(session: AsyncSession) -> bool:
    """Return whether the ``effective_target`` matview holds data.

    A ``WITH NO DATA`` materialized view raises
    ``ObjectNotInPrerequisiteStateError`` on SELECT — and that error
    aborts the surrounding transaction. So before reading the view this
    probes ``pg_class.relispopulated`` (the same guard
    :mod:`app.core.effective` uses for its refresh) and lets the caller
    treat an unpopulated view as "no targets" rather than an error.
    """
    result = await session.execute(
        text("SELECT relispopulated FROM pg_class WHERE relname = :n"),
        {"n": _EFFECTIVE_MATVIEW},
    )
    return bool(result.scalar_one_or_none())


def _sensor_entity(room_id: str, param_name: str) -> str:
    """Map a recipe parameter to its measured-sensor ``entity_id`` tag.

    Recipe params are named for the *setpoint* (``temp_day``); the room's
    matching sensor is the same family without the daypart suffix. By
    convention the InfluxDB ``entity_id`` tag is
    ``{room_id}_{measurement}`` where ``measurement`` is the param's
    leading token (``temp_day`` -> ``temp``, ``vwc_target`` -> ``vwc``).
    """
    measurement = param_name.split("_", 1)[0]
    return f"{room_id}_{measurement}"


async def check_room_tolerances(
    session: AsyncSession,
    influx: _SensorReader,
    room_id: str,
    day_index: int,
) -> list[ToleranceBreach]:
    """Compare a room's effective targets against live sensor values.

    For every effective target on ``(room_id, day_index)`` the matching
    sensor's current value is read from InfluxDB and classified. Only
    out-of-band parameters are returned; in-band ones are silent.

    A parameter whose sensor has no current reading is skipped (not a
    breach) — staleness is the state machine's concern, handled
    separately via :func:`app.core.equipment` / explicit stale-sensor
    lists, not conflated with drift here.

    Args:
        session: Active async session (reads ``effective_target``).
        influx: Sensor reader (an :class:`~app.influx_client.InfluxClient`
            or compatible double).
        room_id: Room to check.
        day_index: Cycle day whose effective targets are evaluated.

    Returns:
        A list of :class:`ToleranceBreach`, one per drifted parameter,
        empty when every measurable parameter is in band — and also
        empty when the ``effective_target`` matview has not been
        populated yet (no recipe approved -> nothing to compare).
    """
    if not await _effective_targets_populated(session):
        log.debug(
            "tolerance_skip_unpopulated_matview",
            room_id=room_id,
            day_index=day_index,
        )
        return []

    targets = await effective_targets_for_day(session, room_id, day_index)
    breaches: list[ToleranceBreach] = []

    for param_name, target in targets.items():
        entity = _sensor_entity(room_id, param_name)
        actual = influx.current_value(entity)
        if actual is None:
            log.debug(
                "tolerance_sensor_no_reading",
                room_id=room_id,
                param_name=param_name,
                entity=entity,
            )
            continue

        tolerance = (
            target.tolerance
            if target.tolerance is not None
            else _DEFAULT_TOLERANCE
        )
        result = classify_drift(actual, float(target.value), tolerance)
        if result.in_band:
            continue

        log.info(
            "tolerance_breach",
            room_id=room_id,
            param_name=param_name,
            drift=result.drift.value,
            actual=actual,
            target=float(target.value),
        )
        breaches.append(
            ToleranceBreach(
                room_id=room_id,
                param_name=param_name,
                day_index=day_index,
                result=result,
            )
        )

    return breaches
