"""Supervisor worker — the AI tick loop (Report + SFW modes).

The supervisor runs every ``supervisor_tick_seconds`` (plan: 5 min
default). Under the ``supervisor_tick`` Postgres advisory lock — so only
one instance ticks even across restarts (plan locked decision #3) — it
walks the configured rooms and, for each:

1. **Skips** the room if it is not a candidate for an AI tick:
   * not HEALTHY (IMPAIRED / CRITICAL rooms suppress LLM ticks — plan
     risk #14; acting on a degraded room means acting on bad inputs),
   * has an open pending approval (don't stack proposals),
   * is muted or paused (operator switches),
   * its light phase is OFF (a dark room is not a steering target),
   * or ``now`` falls inside a no-touch window (plan locked decision
     #14).
2. **Builds a snapshot** (:func:`app.core.snapshot.build_snapshot`) and
   runs the tolerance check. If every measured parameter is in band, it
   records a *nominal* assessment and **skips the LLM entirely** — the
   tolerance bands are what keep the AI quiet during normal operation
   (plan risk #12).
3. Otherwise it builds the deterministic ``allowed_action_set``
   (:func:`app.core.action_set.build_action_set`), calls the LLM with
   the system prompt + snapshot + action set, **strictly parses** the
   reply, and writes an :class:`~app.models.llm_call_log.LLMCallLog` row
   (prompt + raw response + parse outcome + validator errors).

Per-class mode (Phase 8)
------------------------
Once a usable decision is parsed, the supervisor resolves the room's
*effective mode* per proposed change via :func:`app.core.rollout.effective_mode`
(rollout stage by parameter class):

* **report_only** — the AI recommendation is logged + notifiable only;
  no overlay, no command (the structural Report-mode guarantee — the
  report path has no apply call at all).
* **supervised_approval** (SFW) — if the decision carries a non-empty
  proposal *and any* proposed change resolves to ``supervised_approval``,
  the proposal is parked as a :class:`~app.models.pending_approval.PendingApproval`
  via :func:`app.core.sfw.create_pending` (and pushed to Telegram if a
  bot is wired). The supervisor still applies nothing itself — a human
  decides.
* **bounded_auto_adjust** (YOLO) — Phase 9. Each proposed change runs
  through the guardrail validator (:func:`app.core.guardrails.validate_all`);
  an ``apply`` / ``apply_novel`` verdict adds a runtime overlay
  (:func:`app.core.overlays.add_adjustment`) and enqueues a command
  batch (:func:`app.core.command_queue.enqueue_batch`); a ``reject``
  writes a ``guardrail_rejection`` event and runs the same-``AP``
  pattern detector (:func:`app.core.guardrails.detect_rejection_pattern`);
  a ``defer`` is logged and skipped.

Every tick also sweeps expired pending approvals
(:func:`app.core.sfw.expire_stale_pending`).

The session factory, InfluxDB client, HA client and LLM client are all
injected so tests drive the whole tick with fakes; an optional Telegram
bot is injected too (``None`` in tests / when Telegram is unconfigured).
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

from app.config import get_settings
from app.core.action_set import (
    Action,
    ParamCandidate,
    action_set_to_payload,
    build_action_set,
)
from app.core.audit import log_audit
from app.core.command_queue import enqueue_batch
from app.core.coupling_rules import RoomConfig
from app.core.effective import effective_target
from app.core.guardrails import (
    GuardrailDecision,
    detect_rejection_pattern,
    validate_all,
)
from app.core.locks import LockBusyError, advisory_lock
from app.core.no_touch import NoTouchWindow, in_no_touch_window
from app.core.overlays import add_adjustment
from app.core.rollout import class_for_param, effective_mode
from app.core.room_runtime import (
    cycle_day_for,
    get_or_create,
    mark_ticked,
    update_state,
)
from app.core.sfw import create_pending, expire_stale_pending
from app.core.snapshot import SnapshotRequest, build_snapshot
from app.core.state_machine import RoomState
from app.core.tolerance import check_room_tolerances
from app.llm_client import ChatMessage, LLMClientProtocol, parse_decision
from app.models.audit_event import AuditEventType
from app.models.event_log import EventLogEntry, EventSeverity
from app.models.llm_call_log import LLMCallLog, LLMCallOutcome
from app.models.runtime_adjustment import (
    AdjustmentMode,
    AdjustmentSource,
)
from app.models.user import User
from app.prompts import full_system_prompt

if TYPE_CHECKING:
    from contextlib import AbstractAsyncContextManager

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.equipment import RoomContext, _InfluxLike
    from app.models.sensor_snapshot import SensorSnapshot

log = structlog.get_logger(__name__)

#: Advisory-lock name — only one supervisor ticks at a time.
_LOCK_NAME = "supervisor_tick"

#: Worker identity in the event / audit trail. Also the ``users.id`` of
#: the system user the bounded-auto-adjust path attributes overlays to —
#: a ``runtime_adjustment.created_by`` FK must resolve, and an
#: AI-applied overlay has no human creator.
_ACTOR = "supervisor"

#: Display name for the lazily-created supervisor system user.
_SYSTEM_USER_DISPLAY_NAME = "Supervisor (AI worker)"

#: Type alias: a zero-arg callable returning an AsyncSession context manager.
SessionFactory = Callable[[], "AbstractAsyncContextManager[AsyncSession]"]


@dataclass(slots=True)
class RoomTickInput:
    """Per-room configuration the supervisor needs for one tick.

    Attributes:
        room_id: The room.
        room_context: Its equipment + sensor map (drives saturation
            predicates and the snapshot's sensor list).
        recipe_revision_id: The active approved recipe revision id.
        lights_on: Whether the room's lights are currently on. A room
            whose phase is OFF is skipped.
        action_candidates: Explicit action-set candidates (steered
            params + tolerances). When empty the action set is derived
            from the snapshot payload.
        sensor_entities: Extra InfluxDB ``entity_id`` tags to capture in
            the snapshot beyond those implied by ``room_context``.
        room_config: The room's static equipment / coupling map, used by
            the Phase-9 bounded-auto-adjust validator's coupling-rule
            checks. ``None`` means an all-defaults config.
    """

    room_id: str
    room_context: RoomContext
    recipe_revision_id: int
    lights_on: bool = True
    action_candidates: list[ParamCandidate] = field(default_factory=list)
    sensor_entities: Sequence[str] = field(default_factory=tuple)
    room_config: RoomConfig | None = None


@dataclass(slots=True)
class RoomTickResult:
    """The outcome of ticking one room.

    Attributes:
        room_id: The room ticked.
        skipped: ``True`` if the room was skipped before any snapshot.
        skip_reason: Why it was skipped (empty when not skipped).
        snapshot_id: The snapshot built for the room, if any.
        in_band: ``True`` if the tolerance check found no drift (the LLM
            was not called — nominal).
        llm_called: ``True`` if the LLM was actually consulted.
        llm_call_id: The ``llm_call_log`` row id, if a call was made.
        outcome: The :class:`~app.models.llm_call_log.LLMCallOutcome`,
            if a call was made.
        mode: The resolved effective mode for the room's proposal —
            ``report_only`` / ``supervised_approval`` / ``bounded_auto_adjust``
            — set once a decision was parsed.
        pending_id: The :class:`~app.models.pending_approval.PendingApproval`
            id created when the SFW path fired (``None`` otherwise).
        guardrail_decisions: One :class:`~app.core.guardrails.GuardrailDecision`
            per proposed change processed in ``bounded_auto_adjust`` mode
            (empty for Report / SFW ticks).
    """

    room_id: str
    skipped: bool = False
    skip_reason: str = ""
    snapshot_id: int | None = None
    in_band: bool = False
    llm_called: bool = False
    llm_call_id: int | None = None
    outcome: LLMCallOutcome | None = None
    mode: AdjustmentMode | None = None
    pending_id: int | None = None
    guardrail_decisions: list[GuardrailDecision] = field(
        default_factory=list
    )


class Supervisor:
    """AI tick loop running in Report + SFW modes (Phases 7-8).

    Args:
        session_factory: Zero-arg callable yielding an ``AsyncSession``
            async context manager (with the audit HMAC GUCs set, exactly
            as :func:`app.db.open_session` does).
        influx: InfluxDB client (or compatible double) — current-value
            fallback + trend source.
        ha: Home Assistant client (or compatible double / ``None``) —
            preferred current-value source.
        llm: LLM client satisfying :class:`LLMClientProtocol`.
        rooms_provider: Zero-arg callable returning the list of
            :class:`RoomTickInput` to tick. A callable (not a static
            list) so room config can change between ticks.
        no_touch_windows: Configured no-touch windows; a room is skipped
            when the tick time falls inside any of them.
        now_provider: Callable returning "now" — injectable for tests.
        telegram_bot: Optional Telegram approval bot. When set, an SFW
            pending approval is also pushed to Telegram; ``None`` (the
            test / Telegram-unconfigured default) means UI-only routing.
    """

    def __init__(
        self,
        session_factory: SessionFactory,
        influx: _InfluxLike,
        ha: Any,
        llm: LLMClientProtocol,
        *,
        rooms_provider: Callable[[], Sequence[RoomTickInput]],
        no_touch_windows: Sequence[NoTouchWindow] | None = None,
        now_provider: Callable[[], dt.datetime] | None = None,
        telegram_bot: Any = None,
    ) -> None:
        self._session_factory = session_factory
        self._influx = influx
        self._ha = ha
        self._llm = llm
        self._rooms_provider = rooms_provider
        self._no_touch_windows = list(no_touch_windows or [])
        self._now = now_provider or (lambda: dt.datetime.now(dt.UTC))
        self._telegram_bot = telegram_bot
        self._stopped = asyncio.Event()

    # -- tick -------------------------------------------------------------

    async def tick(self) -> list[RoomTickResult]:
        """Run one supervisor tick over every configured room.

        Acquires the ``supervisor_tick`` advisory lock for the whole
        tick; if another supervisor holds it this returns ``[]`` without
        doing work. Each room is processed in its own transaction so a
        failure on one room does not roll back another.

        Returns:
            One :class:`RoomTickResult` per room processed (empty if the
            advisory lock was busy).
        """
        async with self._session_factory() as lock_session:
            try:
                async with advisory_lock(lock_session, _LOCK_NAME):
                    return await self._tick_all_rooms()
            except LockBusyError:
                log.debug("supervisor_lock_busy_skip")
                return []

    async def _tick_all_rooms(self) -> list[RoomTickResult]:
        """Tick each configured room in its own transaction.

        A leading sweep expires any SFW pending approvals past their TTL
        (plan: 90-min pending TTL) so a stale proposal cannot be approved
        long after the snapshot it was built from went cold.
        """
        await self._sweep_expired_pending()

        rooms = list(self._rooms_provider())
        results: list[RoomTickResult] = []
        for room in rooms:
            try:
                async with self._session_factory() as session:
                    result = await self._tick_room(session, room)
                    await session.commit()
            except Exception:  # one room's failure must not kill the tick
                log.exception("supervisor_room_tick_error", room_id=room.room_id)
                results.append(
                    RoomTickResult(
                        room_id=room.room_id,
                        skipped=True,
                        skip_reason="tick_error",
                    )
                )
            else:
                results.append(result)
        log.info(
            "supervisor_tick_complete",
            rooms=len(rooms),
            llm_calls=sum(1 for r in results if r.llm_called),
            pending_created=sum(1 for r in results if r.pending_id is not None),
        )
        return results

    async def _sweep_expired_pending(self) -> int:
        """Expire stale SFW pending approvals — once per tick.

        Runs in its own transaction so an expiry sweep failure cannot
        roll back a room tick (and vice versa).

        Returns:
            The number of pending rows expired.
        """
        try:
            async with self._session_factory() as session:
                expired = await expire_stale_pending(session)
                await session.commit()
        except Exception:  # a sweep failure must not kill the tick
            log.exception("supervisor_pending_sweep_error")
            return 0
        if expired:
            log.info("supervisor_pending_expired", count=expired)
        return expired

    async def _tick_room(
        self, session: AsyncSession, room: RoomTickInput
    ) -> RoomTickResult:
        """Process one room: skip-checks, snapshot, tolerance, maybe LLM."""
        now = self._now()
        runtime = await get_or_create(session, room.room_id)

        skip_reason = self._skip_reason(room, runtime, now)
        if skip_reason is not None:
            log.info(
                "supervisor_room_skipped",
                room_id=room.room_id,
                reason=skip_reason,
            )
            await mark_ticked(session, runtime, at=now)
            return RoomTickResult(
                room_id=room.room_id, skipped=True, skip_reason=skip_reason
            )

        cycle_day = cycle_day_for(runtime, today=now.date())
        snapshot = await build_snapshot(
            session,
            self._influx,
            self._ha,
            SnapshotRequest(
                room_id=room.room_id,
                room_context=room.room_context,
                recipe_revision_id=room.recipe_revision_id,
                cycle_day=cycle_day,
                rollout_stage=runtime.rollout_stage,
                sensor_entities=room.sensor_entities,
                captured_at=now,
            ),
        )

        breaches = await check_room_tolerances(
            session, self._influx, room.room_id, cycle_day
        )
        if not breaches:
            await self._record_nominal(session, room.room_id, snapshot)
            await mark_ticked(session, runtime, at=now)
            return RoomTickResult(
                room_id=room.room_id,
                snapshot_id=snapshot.id,
                in_band=True,
            )

        # Out of band -> consult the LLM.
        action_set = build_action_set(
            snapshot,
            candidates=room.action_candidates or None,
        )
        result = await self._run_llm_report(
            session, room, runtime, snapshot, action_set, breaches, now
        )
        await mark_ticked(session, runtime, at=now)
        await update_state(session, runtime, RoomState.healthy)
        return result

    # -- skip logic -------------------------------------------------------

    def _skip_reason(
        self,
        room: RoomTickInput,
        runtime: Any,
        now: dt.datetime,
    ) -> str | None:
        """Return why a room should be skipped, or ``None`` to proceed.

        The order mirrors the plan: not HEALTHY, open pending, muted,
        paused, lights OFF, in a no-touch window.
        """
        if runtime.current_state != RoomState.healthy.value:
            return f"not_healthy:{runtime.current_state}"
        if runtime.muted:
            return "muted"
        if runtime.paused:
            return "paused"
        if not room.lights_on:
            return "lights_off"
        if in_no_touch_window(now, self._no_touch_windows):
            return "no_touch_window"
        return None

    # -- nominal (in-band) path ------------------------------------------

    async def _record_nominal(
        self, session: AsyncSession, room_id: str, snapshot: SensorSnapshot
    ) -> None:
        """Record a nominal assessment for an in-band room (no LLM call).

        Writes a single ``info_event`` so the dashboard shows the room
        was evaluated and found in band. Deliberately info-severity —
        :class:`~app.workers.alerts.AlertsWorker` keeps info events
        dashboard-only, so a healthy room produces no notification noise.
        """
        session.add(
            EventLogEntry(
                event_type=AuditEventType.info_event,
                severity=EventSeverity.info,
                room_id=room_id,
                summary=(
                    f"Room {room_id} evaluated nominal — all parameters "
                    "in band; LLM not consulted"
                ),
                payload={
                    "snapshot_id": snapshot.id,
                    "assessment": "nominal",
                    "mode": "report_only",
                },
                reason_codes=[],
            )
        )
        await session.flush()
        log.info(
            "supervisor_room_nominal",
            room_id=room_id,
            snapshot_id=snapshot.id,
        )

    # -- LLM report path --------------------------------------------------

    async def _run_llm_report(
        self,
        session: AsyncSession,
        room: RoomTickInput,
        runtime: Any,
        snapshot: SensorSnapshot,
        action_set: list[Action],
        breaches: Sequence[Any],
        now: dt.datetime,
    ) -> RoomTickResult:
        """Call the LLM for an out-of-band room, parse strictly, route it.

        Always writes an :class:`LLMCallLog`. On a usable decision the
        room's effective mode (rollout stage by proposed-change class)
        decides routing: Report mode logs only; SFW mode parks a pending
        approval; bounded-auto-adjust runs the Phase-9 guardrail
        validator and may auto-apply.

        Args:
            session: Active async session.
            room: The room's tick input.
            runtime: The room's :class:`~app.models.room_runtime.RoomRuntime`
                row — its ``rollout_stage`` drives effective-mode
                resolution.
            snapshot: The snapshot built for the room.
            action_set: The deterministic allowed action set.
            breaches: The tolerance breaches that triggered the call.
            now: The tick time — passed to the Phase-9 guardrail
                validator for cool-down / cumulative-window arithmetic.

        Returns:
            A :class:`RoomTickResult` describing the call.
        """
        user_message = self._build_user_message(snapshot, action_set, breaches)
        # full_system_prompt() = system.md (OCS schema + role) ++
        # grow_room_agent_playbook.md (Legacy Ag operational reference).
        # The same string is sent to the LLM AND recorded in the audited
        # prompt payload, so what was reviewed is exactly what was sent.
        system_content = full_system_prompt()
        messages = [
            ChatMessage(role="system", content=system_content),
            ChatMessage(role="user", content=user_message),
        ]
        prompt_payload: dict[str, Any] = {
            "system": system_content,
            "user": user_message,
            "snapshot_id": snapshot.id,
            "allowed_action_set": action_set_to_payload(action_set),
        }

        completion = await self._llm.complete(messages)

        if not completion.ok:
            return await self._log_failed_call(
                session, room, snapshot, prompt_payload, completion
            )

        allowed_action_ids = {a.action_id for a in action_set}
        allowed_params = {a.param_name for a in action_set}
        parse = parse_decision(
            completion.content,
            expected_snapshot_id=snapshot.id,
            allowed_action_ids=allowed_action_ids or None,
            allowed_params=allowed_params or None,
            known_room_ids={room.room_id},
            decision_room_id=room.room_id,
        )

        call_log = LLMCallLog(
            snapshot_id=snapshot.id,
            room_id=room.room_id,
            model=completion.model,
            prompt=prompt_payload,
            response_raw=completion.content,
            response_parsed=(
                parse.decision.model_dump(mode="json")
                if parse.decision is not None
                else None
            ),
            outcome=parse.outcome,
            validator_errors=parse.errors,
            latency_ms=completion.latency_ms,
            prompt_tokens=completion.prompt_tokens,
            completion_tokens=completion.completion_tokens,
        )
        session.add(call_log)
        await session.flush()

        if not (parse.ok and parse.decision is not None):
            await self._record_degraded(
                session, room.room_id, snapshot, call_log, parse.errors
            )
            log.info(
                "supervisor_llm_report",
                room_id=room.room_id,
                snapshot_id=snapshot.id,
                llm_call_id=call_log.id,
                outcome=parse.outcome.value,
            )
            return RoomTickResult(
                room_id=room.room_id,
                snapshot_id=snapshot.id,
                llm_called=True,
                llm_call_id=call_log.id,
                outcome=parse.outcome,
            )

        # A usable decision -> resolve the room's effective mode for it
        # and route accordingly (Report / SFW / bounded-auto-adjust).
        mode = self._resolve_mode(runtime, parse.decision)
        pending_id, guardrail_decisions = await self._route_decision(
            session, room, snapshot, call_log, parse.decision, mode, now
        )

        log.info(
            "supervisor_llm_report",
            room_id=room.room_id,
            snapshot_id=snapshot.id,
            llm_call_id=call_log.id,
            outcome=parse.outcome.value,
            mode=mode.value,
            pending_id=pending_id,
            guardrail_decisions=len(guardrail_decisions),
        )
        return RoomTickResult(
            room_id=room.room_id,
            snapshot_id=snapshot.id,
            llm_called=True,
            llm_call_id=call_log.id,
            outcome=parse.outcome,
            mode=mode,
            pending_id=pending_id,
            guardrail_decisions=guardrail_decisions,
        )

    @staticmethod
    def _resolve_mode(runtime: Any, decision: Any) -> AdjustmentMode:
        """Resolve the effective mode for a decision's proposed changes.

        A decision's ``proposed_changes`` may span parameter classes; the
        room is at one rollout stage. The effective mode for the decision
        is the **most permissive** mode any proposed change resolves to
        (rollout stage by that change's class — see
        :func:`app.core.rollout.effective_mode`). An empty proposal — the
        AI recommends no change — is ``report_only``: there is nothing to
        approve or apply.

        Args:
            runtime: The room's ``RoomRuntime`` row (carries
                ``rollout_stage``).
            decision: The validated :class:`~app.schemas.llm_decision.LLMDecision`.

        Returns:
            The :class:`~app.models.runtime_adjustment.AdjustmentMode` the
            decision should be routed under.
        """
        stage = getattr(runtime, "rollout_stage", "report_only")
        # report_only < supervised_approval < bounded_auto_adjust
        rank = {
            AdjustmentMode.report_only: 0,
            AdjustmentMode.supervised_approval: 1,
            AdjustmentMode.bounded_auto_adjust: 2,
        }
        resolved = AdjustmentMode.report_only
        for change in decision.proposed_changes:
            if change.direction == "no_change" or change.delta == 0.0:
                continue
            mode = effective_mode(stage, class_for_param(change.param_name))
            if rank[mode] > rank[resolved]:
                resolved = mode
        return resolved

    async def _route_decision(
        self,
        session: AsyncSession,
        room: RoomTickInput,
        snapshot: SensorSnapshot,
        call_log: LLMCallLog,
        decision: Any,
        mode: AdjustmentMode,
        now: dt.datetime,
    ) -> tuple[int | None, list[GuardrailDecision]]:
        """Route a usable decision per its resolved effective mode.

        * ``report_only`` — write the report ``event_log`` row only.
        * ``supervised_approval`` — also park a pending approval
          (:func:`app.core.sfw.create_pending`) and, if a Telegram bot is
          wired, push it. The supervisor still applies nothing.
        * ``bounded_auto_adjust`` — run every proposed change through the
          Phase-9 guardrail validator (:meth:`_run_bounded_auto_adjust`):
          ``apply`` / ``apply_novel`` adds an overlay + a command batch,
          ``reject`` writes a ``guardrail_rejection`` event and runs the
          pattern detector, ``defer`` is logged.

        Args:
            session: Active async session.
            room: The room's tick input.
            snapshot: The snapshot the decision was built from.
            call_log: The :class:`LLMCallLog` row for the decision.
            decision: The validated LLM decision.
            mode: The resolved effective mode.
            now: The tick time.

        Returns:
            ``(pending_id, guardrail_decisions)`` — ``pending_id`` is set
            only on the SFW path; ``guardrail_decisions`` is non-empty
            only on the bounded-auto-adjust path.
        """
        # Every routed decision still gets its report event_log row — the
        # dashboard / digest surface is mode-independent.
        await self._record_report(
            session, room.room_id, snapshot, call_log, decision
        )

        if mode is AdjustmentMode.bounded_auto_adjust:
            decisions = await self._run_bounded_auto_adjust(
                session, room, snapshot, call_log, decision, now
            )
            return None, decisions

        if mode is not AdjustmentMode.supervised_approval:
            # report_only applies nothing.
            return None, []

        pending = await create_pending(
            session,
            room_id=room.room_id,
            proposal=self._proposal_payload(decision, snapshot),
            snapshot_id=snapshot.id,
            llm_call_id=call_log.id,
            summary=f"AI proposal for {room.room_id}: {decision.human_summary}",
        )
        await self._notify_telegram(pending)
        return pending.id, []

    async def _run_bounded_auto_adjust(
        self,
        session: AsyncSession,
        room: RoomTickInput,
        snapshot: SensorSnapshot,
        call_log: LLMCallLog,
        decision: Any,
        now: dt.datetime,
    ) -> list[GuardrailDecision]:
        """Run the Phase-9 guardrail validator over a bounded-mode proposal.

        For each non-zero proposed change in the decision, calls
        :func:`app.core.guardrails.validate_all`. The verdict drives the
        side effects:

        * ``apply`` / ``apply_novel`` — :func:`app.core.overlays.add_adjustment`
          adds a runtime overlay (source ``ai_auto``), then the resulting
          effective target is written to HA via one idempotent
          :func:`app.core.command_queue.enqueue_batch`. A
          ``controlled_adjustment`` audit row is written (with the
          ``novel_proposal`` flag when applicable).
        * ``reject`` — a ``guardrail_rejection`` ``event_log`` row +
          audit row are written, then
          :func:`app.core.guardrails.detect_rejection_pattern` runs (a
          run of >= 3 same-``AP`` rejections in 1h escalates to a
          ``formal_deviation``).
        * ``defer`` — an ``info_event`` is logged; nothing applied.

        Args:
            session: Active async session.
            room: The room's tick input.
            snapshot: The snapshot the decision was built from.
            call_log: The :class:`LLMCallLog` row for the decision.
            decision: The validated LLM decision.
            now: The tick time (cool-down / window arithmetic).

        Returns:
            One :class:`GuardrailDecision` per proposed change processed.
        """
        # The bounded path attributes overlays to the supervisor system
        # user; ensure that user row exists (runtime_adjustment.created_by
        # is a FK) before any overlay is added.
        await self._ensure_system_user(session)

        action_set = build_action_set(
            snapshot, candidates=room.action_candidates or None
        )
        day_index = snapshot.cycle_day
        decisions: list[GuardrailDecision] = []
        applied: list[GuardrailDecision] = []

        for change in decision.proposed_changes:
            if change.direction == "no_change" or change.delta == 0.0:
                continue
            verdict = await validate_all(
                session,
                change=change,
                snapshot=snapshot,
                room_config=room.room_config,
                allowed_action_set=action_set,
                room_id=room.room_id,
                now=now,
            )
            decisions.append(verdict)

            if verdict.applied:
                await self._apply_guardrail_change(
                    session, room, snapshot, change, verdict, day_index
                )
                applied.append(verdict)
            elif verdict.rejected:
                await self._record_guardrail_rejection(
                    session, room.room_id, snapshot, call_log, verdict
                )
            else:  # deferred
                await self._record_guardrail_defer(
                    session, room.room_id, snapshot, verdict
                )

        # Apply all overlays first, then write one command batch for the
        # resulting effective targets — the batch is idempotent on the
        # snapshot id so a duplicate tick cannot re-fire it.
        if applied:
            await self._enqueue_guardrail_batch(
                session, room, snapshot, applied, day_index
            )

        log.info(
            "supervisor_bounded_auto_adjust",
            room_id=room.room_id,
            snapshot_id=snapshot.id,
            applied=len(applied),
            rejected=sum(1 for d in decisions if d.rejected),
            deferred=sum(1 for d in decisions if d.deferred),
        )
        return decisions

    @staticmethod
    async def _ensure_system_user(session: AsyncSession) -> None:
        """Lazily create the supervisor system user.

        A bounded-auto-adjust overlay has no human creator, so it is
        attributed to the :data:`_ACTOR` system user. ``runtime_adjustment.created_by``
        is a foreign key to ``users.id``, so that user row must exist.
        It is created on first use (no migration needed — same lazy
        posture as :func:`app.core.room_runtime.get_or_create`).
        """
        if await session.get(User, _ACTOR) is not None:
            return
        session.add(
            User(
                id=_ACTOR,
                display_name=_SYSTEM_USER_DISPLAY_NAME,
                active=True,
            )
        )
        await session.flush()
        log.info("supervisor_system_user_created", user_id=_ACTOR)

    async def _apply_guardrail_change(
        self,
        session: AsyncSession,
        room: RoomTickInput,
        snapshot: SensorSnapshot,
        change: Any,
        verdict: GuardrailDecision,
        day_index: int,
    ) -> None:
        """Apply one guardrail-approved change as a runtime overlay.

        Adds a ``bounded_auto_adjust`` runtime overlay (source
        ``ai_auto``); the command-queue write for the resulting effective
        target is enqueued separately, once, by
        :meth:`_enqueue_guardrail_batch`.
        """
        expires_at = dt.datetime.now(dt.UTC) + dt.timedelta(hours=12)
        await add_adjustment(
            session,
            room_id=room.room_id,
            day_index=day_index,
            param_name=change.param_name,
            delta=verdict.applied_delta,
            source=AdjustmentSource.ai_auto,
            mode=AdjustmentMode.bounded_auto_adjust,
            expires_at=expires_at,
            created_by=_ACTOR,
            snapshot_id=snapshot.id,
            novel_proposal=verdict.novel_proposal,
            reason_codes=list(verdict.reason_codes),
        )
        # controlled_adjustment audit row for the bounded apply — NOT a
        # deviation (plan event taxonomy: controlled_adjustment).
        await log_audit(
            session,
            event_type=AuditEventType.controlled_adjustment,
            actor_id=_ACTOR,
            room_id=room.room_id,
            summary=(
                f"Bounded auto-adjust applied {change.param_name} "
                f"{verdict.applied_delta:+g} for {room.room_id}"
                + (" (novel proposal)" if verdict.novel_proposal else "")
            ),
            params={
                "param_name": change.param_name,
                "delta": verdict.applied_delta,
                "mode": AdjustmentMode.bounded_auto_adjust.value,
                "novel_proposal": verdict.novel_proposal,
                "outcome": verdict.outcome,
            },
            reason_codes=list(verdict.reason_codes),
            snapshot_id=snapshot.id,
        )
        session.add(
            EventLogEntry(
                event_type=AuditEventType.controlled_adjustment,
                severity=EventSeverity.info,
                room_id=room.room_id,
                summary=(
                    f"Bounded auto-adjust: {change.param_name} "
                    f"{verdict.applied_delta:+g}"
                    + (" (novel)" if verdict.novel_proposal else "")
                ),
                payload={
                    "mode": AdjustmentMode.bounded_auto_adjust.value,
                    "snapshot_id": snapshot.id,
                    "param_name": change.param_name,
                    "delta": verdict.applied_delta,
                    "novel_proposal": verdict.novel_proposal,
                },
                reason_codes=list(verdict.reason_codes),
            )
        )
        await session.flush()

    async def _enqueue_guardrail_batch(
        self,
        session: AsyncSession,
        room: RoomTickInput,
        snapshot: SensorSnapshot,
        applied: list[GuardrailDecision],
        day_index: int,
    ) -> None:
        """Enqueue one command batch for every guardrail-applied change.

        The overlays are already in place (and the effective-target
        matview refreshed by :func:`app.core.overlays.add_adjustment`);
        this reads each resulting effective target and enqueues an
        idempotent batch keyed on the snapshot id.
        """
        commands: list[dict[str, Any]] = []
        for verdict in applied:
            value = await effective_target(
                session, room.room_id, day_index, verdict.param_name
            )
            if value is None:
                log.warning(
                    "bounded_auto_adjust_no_effective_target",
                    room_id=room.room_id,
                    param_name=verdict.param_name,
                )
                continue
            entity = (
                f"input_number.{room.room_id}_setpoint_{verdict.param_name}"
            )
            commands.append(
                {
                    "domain": "input_number",
                    "service": "set_value",
                    "target_entity": entity,
                    "service_data": {"entity_id": entity, "value": value},
                    "expected_value": f"{value}",
                    "reason": (
                        f"bounded auto-adjust ({verdict.param_name})"
                    ),
                }
            )
        if commands:
            await enqueue_batch(
                session,
                room_id=room.room_id,
                purpose="bounded_auto_adjust",
                idempotency_key=f"bounded-auto-adjust:{snapshot.id}",
                commands=commands,
                enqueued_by=_ACTOR,
            )

    async def _record_guardrail_rejection(
        self,
        session: AsyncSession,
        room_id: str,
        snapshot: SensorSnapshot,
        call_log: LLMCallLog,
        verdict: GuardrailDecision,
    ) -> None:
        """Write a ``guardrail_rejection`` for a rejected bounded change.

        Writes the audit row first (so the pattern detector counts it),
        an ``event_log`` row for the dashboard, then runs
        :func:`app.core.guardrails.detect_rejection_pattern` for every
        ``AP-*`` id the rejection cited — a run of three same-``AP``
        rejections in an hour escalates to a ``formal_deviation``.
        """
        await log_audit(
            session,
            event_type=AuditEventType.guardrail_rejection,
            actor_id=_ACTOR,
            room_id=room_id,
            summary=(
                f"Bounded auto-adjust rejected {verdict.param_name} "
                f"for {room_id}: {verdict.detail}"
            ),
            params={
                "param_name": verdict.param_name,
                "outcome": verdict.outcome,
                "detail": verdict.detail,
            },
            reason_codes=list(verdict.reason_codes),
            snapshot_id=snapshot.id,
            llm_call_id=call_log.id,
        )
        session.add(
            EventLogEntry(
                event_type=AuditEventType.guardrail_rejection,
                severity=EventSeverity.warning,
                room_id=room_id,
                summary=(
                    f"Guardrail rejection: {verdict.param_name} — "
                    f"{verdict.detail[:400]}"
                ),
                payload={
                    "mode": AdjustmentMode.bounded_auto_adjust.value,
                    "snapshot_id": snapshot.id,
                    "param_name": verdict.param_name,
                    "outcome": verdict.outcome,
                },
                reason_codes=list(verdict.reason_codes),
            )
        )
        await session.flush()

        # Pattern detection: a run of same-AP rejections is escalated.
        for code in verdict.reason_codes:
            if code.startswith("AP-"):
                await detect_rejection_pattern(session, room_id, code)

    async def _record_guardrail_defer(
        self,
        session: AsyncSession,
        room_id: str,
        snapshot: SensorSnapshot,
        verdict: GuardrailDecision,
    ) -> None:
        """Write an ``info_event`` for a deferred bounded change.

        A deferral (cool-down active, or a no-touch window) is neither an
        apply nor a rejection — it is logged info-severity and nothing
        is applied.
        """
        session.add(
            EventLogEntry(
                event_type=AuditEventType.info_event,
                severity=EventSeverity.info,
                room_id=room_id,
                summary=(
                    f"Bounded auto-adjust deferred {verdict.param_name} "
                    f"for {room_id}: {verdict.detail[:400]}"
                ),
                payload={
                    "mode": AdjustmentMode.bounded_auto_adjust.value,
                    "snapshot_id": snapshot.id,
                    "param_name": verdict.param_name,
                    "outcome": verdict.outcome,
                },
                reason_codes=list(verdict.reason_codes),
            )
        )
        await session.flush()

    @staticmethod
    def _proposal_payload(decision: Any, snapshot: SensorSnapshot) -> dict[str, Any]:
        """Build the ``pending_approval.proposal`` payload from a decision.

        The decision is serialised as-is, plus a ``day_index`` key — the
        snapshot's cycle day — so :func:`app.core.sfw.approve_pending` can
        place the overlay on the right recipe day without re-deriving it.
        """
        payload = decision.model_dump(mode="json")
        payload["day_index"] = snapshot.cycle_day
        return payload

    async def _notify_telegram(self, pending: Any) -> None:
        """Push an SFW pending approval to Telegram, if a bot is wired.

        A Telegram send failure is logged but never fails the tick — the
        pending row is already persisted and the UI can still decide it.
        """
        if self._telegram_bot is None:
            return
        try:
            await self._telegram_bot.send_approval(pending)
        except Exception:  # Telegram down must not fail the tick
            log.exception(
                "supervisor_telegram_send_failed", pending_id=pending.id
            )

    @staticmethod
    def _build_user_message(
        snapshot: SensorSnapshot,
        action_set: list[Action],
        breaches: Sequence[Any],
    ) -> str:
        """Render the user message — snapshot + action set + drift summary.

        The snapshot payload is the model's sole factual input; the
        action set is the boring-safe menu; the drift list points the
        model at what is out of band.
        """
        import json  # noqa: PLC0415 — local import keeps module surface small

        drift = [
            {
                "param_name": getattr(b, "param_name", None),
                "drift": getattr(getattr(b, "result", None), "drift", None)
                and b.result.drift.value,
                "actual": getattr(getattr(b, "result", None), "actual", None),
                "target": getattr(getattr(b, "result", None), "target", None),
            }
            for b in breaches
        ]
        body = {
            "instruction": (
                "Assess this cultivation room. You are in Report mode: "
                "observe and explain only. Echo snapshot_id exactly. "
                "Return ONLY the ocs.llm_decision.v1 JSON object."
            ),
            "snapshot": snapshot.payload,
            "tolerance_breaches": drift,
            "allowed_action_set": action_set_to_payload(action_set),
        }
        return json.dumps(body, default=str, indent=2)

    async def _log_failed_call(
        self,
        session: AsyncSession,
        room: RoomTickInput,
        snapshot: SensorSnapshot,
        prompt_payload: dict[str, Any],
        completion: Any,
    ) -> RoomTickResult:
        """Record an LLM transport / API failure as a degraded call."""
        outcome = (
            LLMCallOutcome.timeout
            if completion.timed_out
            else LLMCallOutcome.api_error
        )
        call_log = LLMCallLog(
            snapshot_id=snapshot.id,
            room_id=room.room_id,
            model=completion.model,
            prompt=prompt_payload,
            response_raw=None,
            response_parsed=None,
            outcome=outcome,
            validator_errors=[completion.error or "LLM call failed"],
            latency_ms=completion.latency_ms,
        )
        session.add(call_log)
        await session.flush()
        await self._record_degraded(
            session,
            room.room_id,
            snapshot,
            call_log,
            [completion.error or "LLM call failed"],
        )
        log.warning(
            "supervisor_llm_call_failed",
            room_id=room.room_id,
            outcome=outcome.value,
            error=completion.error,
        )
        return RoomTickResult(
            room_id=room.room_id,
            snapshot_id=snapshot.id,
            llm_called=True,
            llm_call_id=call_log.id,
            outcome=outcome,
        )

    async def _record_report(
        self,
        session: AsyncSession,
        room_id: str,
        snapshot: SensorSnapshot,
        call_log: LLMCallLog,
        decision: Any,
    ) -> None:
        """Write the ``event_log`` row for a successfully-parsed report.

        Report mode: the AI recommendation is *logged and notifiable*
        only. Severity follows the decision — ``warning`` when the model
        flags ``requires_human``, else ``info``. No overlay or command is
        written; that is the structural report-mode guarantee.
        """
        severity = (
            EventSeverity.warning if decision.requires_human else EventSeverity.info
        )
        session.add(
            EventLogEntry(
                event_type=AuditEventType.info_event,
                severity=severity,
                room_id=room_id,
                summary=f"AI report ({room_id}): {decision.human_summary[:512]}",
                payload={
                    "mode": "report_only",
                    "snapshot_id": snapshot.id,
                    "llm_call_id": call_log.id,
                    "assessment": decision.assessment,
                    "recommended_action_id": decision.recommended_action_id,
                    "proposed_changes": [
                        c.model_dump(mode="json")
                        for c in decision.proposed_changes
                    ],
                    "confidence": decision.confidence,
                    "requires_human": decision.requires_human,
                },
                reason_codes=list(decision.reason_codes),
            )
        )
        await session.flush()

    async def _record_degraded(
        self,
        session: AsyncSession,
        room_id: str,
        snapshot: SensorSnapshot,
        call_log: LLMCallLog,
        errors: Sequence[str],
    ) -> None:
        """Write a ``system_warning`` for a rejected / failed LLM call.

        A degraded LLM is operationally a warning (plan event taxonomy):
        the supervisor could not get a usable proposal, so the room
        simply has no AI report this tick — still no control change.
        """
        session.add(
            EventLogEntry(
                event_type=AuditEventType.system_warning,
                severity=EventSeverity.warning,
                room_id=room_id,
                summary=(
                    f"AI report unavailable for {room_id}: "
                    f"{call_log.outcome.value}"
                ),
                payload={
                    "mode": "report_only",
                    "snapshot_id": snapshot.id,
                    "llm_call_id": call_log.id,
                    "outcome": call_log.outcome.value,
                    "errors": list(errors),
                },
                reason_codes=["llm_degraded"],
            )
        )
        await session.flush()

    # -- run loop ---------------------------------------------------------

    async def run_forever(self, interval: float | None = None) -> None:
        """Loop :meth:`tick`, sleeping ``interval`` seconds between ticks.

        Cancellation-safe: an ``asyncio.CancelledError`` (or a
        :meth:`stop` call) breaks the loop cleanly. One tick raising
        never kills the loop.

        Args:
            interval: Seconds between ticks. Defaults to
                ``settings.supervisor_tick_seconds``.
        """
        period = (
            interval
            if interval is not None
            else float(get_settings().supervisor_tick_seconds)
        )
        log.info("supervisor_started", interval=period)
        try:
            while not self._stopped.is_set():
                try:
                    await self.tick()
                except Exception:  # never let one tick kill the loop
                    log.exception("supervisor_tick_error")
                try:
                    await asyncio.wait_for(
                        self._stopped.wait(), timeout=period
                    )
                except TimeoutError:
                    continue
        except asyncio.CancelledError:
            log.info("supervisor_cancelled")
            raise
        finally:
            log.info("supervisor_stopped")

    def stop(self) -> None:
        """Signal :meth:`run_forever` to exit after the current tick."""
        self._stopped.set()
