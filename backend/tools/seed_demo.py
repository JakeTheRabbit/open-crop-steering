"""Seed demo data so the UI has something to render.

Not part of the product — a developer convenience for poking around the
add-on without a live Home Assistant. Run it INSIDE the container:

    docker exec ocs sh -c 'set -a; . /run/ocs.env; python3 \\
        /opt/ocs-tools/seed_demo.py'

It is idempotent: if demo users already exist it exits without doing
anything. Everything is created through the real domain helpers
(``recipe_store``, ``overlays``, ``log_audit``) so the audit chain,
HMAC signing and effective-target view all populate exactly as they
would in production.
"""

from __future__ import annotations

import asyncio
import datetime as dt

from app.config import get_settings
from app.core.audit import log_audit
from app.core.overlays import add_adjustment
from app.core.recipe_store import approve_revision, create_revision
from app.db import dispose_engine, get_session_factory
from app.models.audit_event import AuditEventType
from app.models.event_log import EventLogEntry, EventSeverity
from app.models.pending_approval import PendingApproval, PendingStatus
from app.models.room_runtime import RoomRuntime
from app.models.runtime_adjustment import AdjustmentMode, AdjustmentSource
from app.models.sensor_snapshot import SensorSnapshot
from app.models.user import RoleName, User, UserRole
from app.presets.cannabis_12week import build_cannabis_12week
from sqlalchemy import func, select, text

UTC = dt.UTC

# Each tuple is: id, display name, email, role.
DEMO_USERS: tuple[tuple[str, str, str | None, RoleName | None], ...] = (
    ("demo-operator", "Olivia Operator", "operator@example.com", RoleName.operator),
    ("demo-cultivator", "Carl Cultivator", "cultivator@example.com", RoleName.cultivator),
    ("demo-qap", "Quinn QAP", "qap@example.com", RoleName.qap),
    ("demo-admin", "Avery Admin", "admin@example.com", RoleName.admin),
    ("ai-supervisor", "AI Supervisor", None, None),
)

# Each tuple is: room id, room name, cycle-day offset from today.
DEMO_ROOMS: tuple[tuple[str, str, int], ...] = (
    ("f1", "Flower Room 1", 32),
    ("f2", "Flower Room 2", 18),
)


async def _already_seeded(session) -> bool:  # type: ignore[no-untyped-def]
    result = await session.execute(
        select(func.count()).select_from(User).where(User.id.like("demo-%"))
    )
    return (result.scalar_one() or 0) > 0


async def seed() -> None:
    settings = get_settings()
    factory = get_session_factory()

    async with factory() as session:
        # Mirror app.db.get_session(): the audit-chain trigger needs the
        # HMAC key in a session GUC or every audit INSERT fails.
        for key_id, key_hex in settings.hmac_keys.items():
            await session.execute(
                text(f"SELECT set_config('audit.hmac_key_{int(key_id)}', :v, false)"),
                {"v": key_hex},
            )

        if await _already_seeded(session):
            print("demo data already present — nothing to do.")
            return

        # --- users + roles -------------------------------------------------
        for uid, name, email, _role in DEMO_USERS:
            session.add(User(id=uid, display_name=name, email=email))
        await session.flush()
        for uid, _name, _email, role in DEMO_USERS:
            if role is not None:
                session.add(UserRole(user_id=uid, role_name=role, granted_by="demo-admin"))
        await session.flush()
        print(f"users: {len(DEMO_USERS)}")

        # --- rooms: runtime state + approved cannabis recipe ---------------
        today = dt.datetime.now(UTC).date()
        for room_id, room_name, day_offset in DEMO_ROOMS:
            session.add(
                RoomRuntime(
                    room_id=room_id,
                    rollout_stage="report_only",
                    cycle_start_date=today - dt.timedelta(days=day_offset),
                    current_state="healthy",
                    last_tick_at=dt.datetime.now(UTC) - dt.timedelta(minutes=4),
                )
            )
            await session.flush()

            params = build_cannabis_12week()
            revision = await create_revision(
                session,
                room_id=room_id,
                name=f"{room_name} — cannabis 12-week (demo)",
                params=params,
                created_by="demo-cultivator",
                notes="Seeded demo recipe — cannabis_12week preset.",
            )
            await approve_revision(session, revision.id, "demo-qap")
            print(f"room {room_id}: runtime + recipe v{revision.version} ({len(params)} params) approved")

        # --- a couple of active AI overlays on f1 (cycle day 32) ----------
        expires = dt.datetime.now(UTC) + dt.timedelta(hours=4)
        await add_adjustment(
            session,
            room_id="f1",
            day_index=32,
            param_name="temp_day",
            delta=-0.4,
            source=AdjustmentSource.ai_auto,
            mode=AdjustmentMode.bounded_auto_adjust,
            expires_at=expires,
            created_by="ai-supervisor",
            reason_codes=["SAT-AC"],
        )
        await add_adjustment(
            session,
            room_id="f1",
            day_index=32,
            param_name="rh_day",
            delta=-1.5,
            source=AdjustmentSource.ai_auto,
            mode=AdjustmentMode.bounded_auto_adjust,
            expires_at=expires,
            created_by="ai-supervisor",
            reason_codes=["EC-004"],
        )
        print("overlays: 2 active on f1")

        # --- a sensor snapshot + pending SFW approval for f2 --------------
        snapshot = SensorSnapshot(
            room_id="f2",
            payload={
                "env": {"temp": 27.9, "rh": 61.2, "co2": 1080, "vpd": 1.31},
                "equipment_status": {"ac": "ok", "dehu": "SAT-DEHU", "co2": "ok"},
            },
            recipe_revision_id=1,
            cycle_day=18,
            rollout_stage="report_only",
        )
        session.add(snapshot)
        await session.flush()
        session.add(
            PendingApproval(
                room_id="f2",
                proposal={
                    "schema_version": "ocs.llm_decision.v1",
                    "snapshot_id": snapshot.id,
                    "recommended_action_id": "rh_day-down-0.2",
                    "proposed_changes": [
                        {"param": "rh_day", "delta": -2.0, "unit": "%"}
                    ],
                    "confidence": 0.74,
                    "reason_codes": ["SAT-DEHU", "AP-06"],
                },
                snapshot_id=snapshot.id,
                summary="Lower RH target 2% — dehumidifier trending to saturation.",
                status=PendingStatus.open,
                expires_at=dt.datetime.now(UTC) + dt.timedelta(minutes=75),
            )
        )
        print("pending approval: 1 open on f2")

        # --- event log: info, warning, and one formal deviation -----------
        now = dt.datetime.now(UTC)
        session.add_all(
            [
                EventLogEntry(
                    occurred_at=now - dt.timedelta(hours=6),
                    event_type=AuditEventType.info_event,
                    severity=EventSeverity.info,
                    room_id="f1",
                    summary="Lights-on setpoints applied for Flower Room 1.",
                    payload={"params_written": 11},
                ),
                EventLogEntry(
                    occurred_at=now - dt.timedelta(hours=2),
                    event_type=AuditEventType.system_warning,
                    severity=EventSeverity.warning,
                    room_id="f2",
                    summary="AC running high ≥15 min with positive temp slope.",
                    payload={"fan": "10/10", "minutes": 17},
                    reason_codes=["SAT-AC"],
                ),
                EventLogEntry(
                    occurred_at=now - dt.timedelta(minutes=40),
                    event_type=AuditEventType.formal_deviation,
                    severity=EventSeverity.critical,
                    room_id="f2",
                    summary="Dehumidifier saturated >30 min — RH out of band with crop impact.",
                    payload={"rh_actual": 64.8, "rh_target": 60.0, "minutes": 34},
                    reason_codes=["SAT-DEHU"],
                ),
            ]
        )

        # --- a couple of extra audit rows for a fuller /audit page --------
        await log_audit(
            session,
            event_type=AuditEventType.user_role_changed,
            actor_id="demo-admin",
            actor_role="admin",
            summary="Granted QAP role to Quinn QAP.",
            params={"target": "demo-qap", "role": "qap"},
        )
        await log_audit(
            session,
            event_type=AuditEventType.system_warning,
            actor_id="ai-supervisor",
            room_id="f2",
            summary="Dehumidifier saturation detected (SAT-DEHU).",
            reason_codes=["SAT-DEHU"],
        )
        print("event_log: 3 rows; extra audit rows: 2")

        await session.commit()
        print("\ndemo data committed.")

    await dispose_engine()


if __name__ == "__main__":
    asyncio.run(seed())
