"""Integration tests for :mod:`app.core.guardrails` — the decision tree.

Runs against real Postgres (the validator reads / writes the
``cumulative_delta`` table). Covered, in plan locked-decision-#13 order:

* in-set + clean + in-bounds -> ``apply``;
* out-of-set + clean + in-bounds -> ``apply_novel`` with the cumulative
  budget halved;
* out-of-bounds -> ``reject`` + a cool-down on the ``cumulative_delta``
  row;
* a cumulative-cap breach -> ``reject``;
* an active cool-down -> ``defer``;
* a no-touch window -> ``defer``;
* the saturation+anti-pattern combination (SAT-AC + AP-02) -> ``reject``
  citing both ids;
* a coupling-rule violation -> ``reject`` citing the EC id.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from app.core.action_set import Action
from app.core.guardrails import (
    CLASS_GUARDRAILS,
    OUTCOME_APPLY,
    OUTCOME_APPLY_NOVEL,
    OUTCOME_DEFER,
    OUTCOME_REJECT,
    validate_all,
)
from app.core.rollout import ParamClass
from app.models.cumulative_delta import CumulativeDelta
from app.schemas.llm_decision import ProposedChange
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

pytestmark = pytest.mark.integration


# --------------------------------------------------------------------- fixtures


@pytest_asyncio.fixture(autouse=True)
async def _clean_cumulative(async_engine: AsyncEngine) -> AsyncIterator[None]:
    """Clear ``cumulative_delta`` rows for the rooms these tests use."""
    from sqlalchemy.ext.asyncio import async_sessionmaker  # noqa: PLC0415

    maker = async_sessionmaker(async_engine, expire_on_commit=False)
    async with maker() as cleanup:
        await cleanup.execute(
            text("DELETE FROM cumulative_delta WHERE room_id LIKE 'gr-%'")
        )
        await cleanup.commit()
    yield


class _FakeSnapshot:
    """Minimal snapshot stand-in — an ``id`` + a ``payload`` dict."""

    def __init__(self, payload: dict, snapshot_id: int = 1) -> None:
        self.id = snapshot_id
        self.payload = payload


def _change(
    param_name: str, delta: float, direction: str | None = None
) -> ProposedChange:
    """Build a :class:`ProposedChange` (direction inferred from delta)."""
    if direction is None:
        direction = (
            "increase" if delta > 0 else "decrease" if delta < 0 else "no_change"
        )
    return ProposedChange(
        param_name=param_name, direction=direction, delta=delta
    )


def _action(param_name: str, delta: float) -> Action:
    """Build an :class:`Action` for the allowed-action-set argument."""
    direction = "increase" if delta > 0 else "decrease"
    return Action(
        action_id=f"act-{param_name}-{direction}",
        param_name=param_name,
        param_class=ParamClass.b,
        direction=direction,
        delta=delta,
        current_value=None,
        resulting_value=None,
        description="test action",
    )


async def _cumulative(
    session: AsyncSession, room_id: str, param_name: str
) -> CumulativeDelta | None:
    """Fetch the ``cumulative_delta`` row for ``(room, param)``."""
    return (
        await session.execute(
            select(CumulativeDelta).where(
                CumulativeDelta.room_id == room_id,
                CumulativeDelta.param_name == param_name,
            )
        )
    ).scalar_one_or_none()


# --------------------------------------------------------------------- in-set


async def test_in_set_clean_change_applies(
    async_engine: AsyncEngine, session: AsyncSession
) -> None:
    """A change in the allowed set, in bounds, clean -> apply."""
    snapshot = _FakeSnapshot(
        {"room_id": "gr-r1", "setpoints": {"temp_day": 26.0}}
    )
    change = _change("temp_day", -0.2)
    decision = await validate_all(
        session,
        change=change,
        snapshot=snapshot,
        room_config=None,
        allowed_action_set=[_action("temp_day", -0.2)],
        room_id="gr-r1",
    )
    await session.commit()

    assert decision.outcome == OUTCOME_APPLY
    assert decision.applied is True
    assert decision.novel_proposal is False
    assert decision.applied_delta == pytest.approx(-0.2)

    # The cumulative-delta row recorded the applied delta.
    row = await _cumulative(session, "gr-r1", "temp_day")
    assert row is not None
    assert row.sum_delta_24h == pytest.approx(-0.2)
    assert row.sum_delta_7d == pytest.approx(-0.2)


# --------------------------------------------------------------- out-of-set


async def test_out_of_set_in_bounds_applies_novel_with_halved_budget(
    async_engine: AsyncEngine, session: AsyncSession
) -> None:
    """An out-of-set but in-bounds clean change -> apply_novel, budget halved.

    Class B's 24h cap is 2.0; a novel proposal gets only the halved
    budget (1.0). A -0.6 delta fits the halved budget and applies; a
    second -0.6 would breach it.
    """
    snapshot = _FakeSnapshot(
        {"room_id": "gr-r2", "setpoints": {"temp_day": 26.0}}
    )
    # Action set offers -0.2; the model proposes -0.6 -> not in the set.
    decision = await validate_all(
        session,
        change=_change("temp_day", -0.6),
        snapshot=snapshot,
        room_config=None,
        allowed_action_set=[_action("temp_day", -0.2)],
        room_id="gr-r2",
    )
    await session.commit()

    assert decision.outcome == OUTCOME_APPLY_NOVEL
    assert decision.novel_proposal is True
    assert "novel_proposal" in decision.reason_codes

    # A second -0.6 (sum -1.2) breaches the halved 1.0 budget -> reject.
    second = await validate_all(
        session,
        change=_change("temp_day", -0.6),
        snapshot=snapshot,
        room_config=None,
        allowed_action_set=[_action("temp_day", -0.2)],
        room_id="gr-r2",
    )
    await session.commit()
    assert second.outcome == OUTCOME_REJECT
    assert "cumulative_cap" in second.reason_codes


# ----------------------------------------------------------- out-of-bounds


async def test_out_of_bounds_rejects_and_sets_cooldown(
    async_engine: AsyncEngine, session: AsyncSession
) -> None:
    """A change whose resulting value is out of bounds -> reject + cool-down.

    temp_day absolute bounds are [15, 35]; from 34.5 a +2.0 step yields
    36.5, outside the upper bound.
    """
    snapshot = _FakeSnapshot(
        {"room_id": "gr-r3", "setpoints": {"temp_day": 34.5}}
    )
    decision = await validate_all(
        session,
        change=_change("temp_day", 2.0),
        snapshot=snapshot,
        room_config=None,
        allowed_action_set=[_action("temp_day", 2.0)],
        room_id="gr-r3",
    )
    await session.commit()

    assert decision.outcome == OUTCOME_REJECT
    assert "out_of_bounds" in decision.reason_codes
    assert decision.cooldown_until is not None

    # The cool-down landed on the cumulative-delta row.
    row = await _cumulative(session, "gr-r3", "temp_day")
    assert row is not None
    assert row.cooldown_until is not None
    assert row.cooldown_reason == "out_of_bounds"


# ----------------------------------------------------------- cumulative cap


async def test_cumulative_cap_breach_rejects(
    async_engine: AsyncEngine, session: AsyncSession
) -> None:
    """A change pushing the rolling 24h sum past its cap -> reject.

    Class B's 24h cap is 2.0; pre-seed the row at 1.8 so an in-set 0.5
    step (sum 2.3) breaches it.
    """
    session.add(
        CumulativeDelta(
            room_id="gr-r4",
            param_name="temp_day",
            sum_delta_24h=1.8,
            sum_delta_7d=1.8,
        )
    )
    await session.flush()

    snapshot = _FakeSnapshot(
        {"room_id": "gr-r4", "setpoints": {"temp_day": 26.0}}
    )
    decision = await validate_all(
        session,
        change=_change("temp_day", 0.5),
        snapshot=snapshot,
        room_config=None,
        allowed_action_set=[_action("temp_day", 0.5)],
        room_id="gr-r4",
    )
    await session.commit()

    assert decision.outcome == OUTCOME_REJECT
    assert "cumulative_cap" in decision.reason_codes


# ------------------------------------------------------------- cool-down


async def test_active_cooldown_defers(
    async_engine: AsyncEngine, session: AsyncSession
) -> None:
    """A change for a parameter in an active cool-down window -> defer."""
    future = dt.datetime.now(dt.UTC) + dt.timedelta(minutes=30)
    session.add(
        CumulativeDelta(
            room_id="gr-r5",
            param_name="temp_day",
            sum_delta_24h=0.0,
            sum_delta_7d=0.0,
            cooldown_until=future,
            cooldown_reason="prior_rejection",
        )
    )
    await session.flush()

    snapshot = _FakeSnapshot(
        {"room_id": "gr-r5", "setpoints": {"temp_day": 26.0}}
    )
    decision = await validate_all(
        session,
        change=_change("temp_day", -0.2),
        snapshot=snapshot,
        room_config=None,
        allowed_action_set=[_action("temp_day", -0.2)],
        room_id="gr-r5",
    )
    await session.commit()

    assert decision.outcome == OUTCOME_DEFER
    assert "cooldown" in decision.reason_codes
    # A deferral applies nothing — the sum is unchanged.
    row = await _cumulative(session, "gr-r5", "temp_day")
    assert row is not None
    assert row.sum_delta_24h == pytest.approx(0.0)


# ----------------------------------------------------------- no-touch window


async def test_no_touch_window_defers(
    async_engine: AsyncEngine, session: AsyncSession
) -> None:
    """A clean change during a no-touch window -> defer (nothing applied)."""
    snapshot = _FakeSnapshot(
        {"room_id": "gr-r6", "setpoints": {"temp_day": 26.0}}
    )
    decision = await validate_all(
        session,
        change=_change("temp_day", -0.2),
        snapshot=snapshot,
        room_config=None,
        allowed_action_set=[_action("temp_day", -0.2)],
        room_id="gr-r6",
        no_touch=True,
    )
    await session.commit()

    assert decision.outcome == OUTCOME_DEFER
    assert "no_touch" in decision.reason_codes
    row = await _cumulative(session, "gr-r6", "temp_day")
    assert row is not None
    assert row.sum_delta_24h == pytest.approx(0.0)


# ------------------------------------------------- saturation + anti-pattern


async def test_saturation_anti_pattern_combo_rejects(
    async_engine: AsyncEngine, session: AsyncSession
) -> None:
    """The canonical SAT-AC + AP-02 combo -> reject citing both ids.

    Lowering temp while the AC is saturated is the plan's canonical
    saturation+anti-pattern rejection; reason_codes must contain BOTH
    ``SAT-AC`` and ``AP-02``.
    """
    snapshot = _FakeSnapshot(
        {
            "room_id": "gr-r7",
            "setpoints": {"temp_day": 26.0},
            "equipment_status": {"ac": "SAT-AC"},
        }
    )
    decision = await validate_all(
        session,
        change=_change("temp_day", -0.2),
        snapshot=snapshot,
        room_config=None,
        allowed_action_set=[_action("temp_day", -0.2)],
        room_id="gr-r7",
    )
    await session.commit()

    assert decision.outcome == OUTCOME_REJECT
    assert "SAT-AC" in decision.reason_codes
    assert "AP-02" in decision.reason_codes
    assert decision.cooldown_until is not None


# ------------------------------------------------------------- coupling rule


async def test_coupling_violation_rejects(
    async_engine: AsyncEngine, session: AsyncSession
) -> None:
    """A change that violates a coupling rule -> reject citing the EC id.

    A drain% cut taking runoff below the ~10% mandatory minimum trips
    EC-011.
    """
    snapshot = _FakeSnapshot(
        {"room_id": "gr-r8", "setpoints": {"drain_pct": 12.0}}
    )
    decision = await validate_all(
        session,
        change=_change("drain_pct", -8.0),
        snapshot=snapshot,
        room_config=None,
        allowed_action_set=[_action("drain_pct", -8.0)],
        room_id="gr-r8",
    )
    await session.commit()

    assert decision.outcome == OUTCOME_REJECT
    assert "EC-011" in decision.reason_codes


# --------------------------------------------------------------- class E


async def test_class_e_param_refused_outright(
    async_engine: AsyncEngine, session: AsyncSession
) -> None:
    """A Class-E parameter is refused — the AI never writes admin state."""
    snapshot = _FakeSnapshot({"room_id": "gr-r9", "setpoints": {}})
    decision = await validate_all(
        session,
        change=_change("rollout_stage", 1.0),
        snapshot=snapshot,
        room_config=None,
        allowed_action_set=None,
        room_id="gr-r9",
    )
    await session.commit()

    assert decision.outcome == OUTCOME_REJECT
    assert "class_e" in decision.reason_codes


# ----------------------------------------------------- per-class cool-down


async def test_class_d_gets_the_longer_cooldown(
    async_engine: AsyncEngine, session: AsyncSession
) -> None:
    """A Class-D rejection gets the 120-min cool-down, not the 60-min one."""
    snapshot = _FakeSnapshot(
        {"room_id": "gr-r10", "setpoints": {"tank_ph": 6.0}}
    )
    before = dt.datetime.now(dt.UTC)
    # tank_ph bounds are [5.0, 7.0]; from 6.0 a +2.0 step yields 8.0.
    decision = await validate_all(
        session,
        change=_change("tank_ph", 2.0),
        snapshot=snapshot,
        room_config=None,
        allowed_action_set=[_action("tank_ph", 2.0)],
        room_id="gr-r10",
    )
    await session.commit()

    assert decision.outcome == OUTCOME_REJECT
    assert decision.cooldown_until is not None
    # Class D cool-down is 120 min — comfortably more than 90.
    delta = decision.cooldown_until - before
    assert delta > dt.timedelta(minutes=90)
    assert CLASS_GUARDRAILS[ParamClass.d].cooldown == dt.timedelta(
        minutes=120
    )
