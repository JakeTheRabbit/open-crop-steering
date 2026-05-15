"""Tamper-evident audit log.

Append-only by DB-level triggers (UPDATE/DELETE blocked). Each row
carries an HMAC-SHA256 signature over a canonical payload that includes
the previous row's HMAC, forming a chain. Verifying the chain detects
any inserted, deleted, or modified row.

The HMAC key is selected by ``key_id``. Old rows keep their original
key; rotation creates a new ``key_id`` going forward. Verifiers walk
the key history. Keys live in env vars / mounted secrets, NEVER in the
DB itself, so a DB compromise doesn't yield the keys.

Daily seal records (see ``daily_seal``) capture the chain head for that
day and get exported off-box (encrypted) so long-running tampering of
historical rows is detectable from outside the system.
"""

from __future__ import annotations

import datetime as dt
import enum

from sqlalchemy import (
    BigInteger,
    DateTime,
    Integer,
    LargeBinary,
    String,
    func,
)
from sqlalchemy import Enum as SqlEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class AuditEventType(enum.StrEnum):
    # Operational event taxonomy (see plan v3 #15)
    info_event = "info_event"
    controlled_adjustment = "controlled_adjustment"
    system_warning = "system_warning"
    guardrail_rejection = "guardrail_rejection"
    formal_deviation = "formal_deviation"
    critical_incident = "critical_incident"

    # Domain action types that always need an audit trail
    recipe_revision_created = "recipe_revision_created"
    recipe_revision_approved = "recipe_revision_approved"
    runtime_adjustment_added = "runtime_adjustment_added"
    runtime_adjustment_reverted = "runtime_adjustment_reverted"
    user_role_changed = "user_role_changed"
    rollout_advanced = "rollout_advanced"
    deviation_acknowledged = "deviation_acknowledged"
    audit_export = "audit_export"
    hmac_key_rotated = "hmac_key_rotated"


class AuditEvent(Base):
    __tablename__ = "audit_event"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    occurred_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True, nullable=False
    )

    event_type: Mapped[AuditEventType] = mapped_column(
        SqlEnum(
            AuditEventType,
            name="audit_event_type",
            native_enum=True,
            create_type=False,
        ),
        index=True,
        nullable=False,
    )
    actor_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    actor_role: Mapped[str | None] = mapped_column(String(32), nullable=True)

    room_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )

    params: Mapped[dict] = mapped_column(
        JSONB, default=dict, server_default="{}", nullable=False
    )
    reason_codes: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default="[]", nullable=False
    )
    summary: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    # Loose foreign-key-ish references — kept loose because audit must
    # outlive any schema change to its referents.
    recipe_revision_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    runtime_adjustment_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    command_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    snapshot_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    llm_call_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Chain fields — populated by BEFORE INSERT trigger
    key_id: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    prev_event_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    hmac: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
