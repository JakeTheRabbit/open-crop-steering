"""SQLAlchemy ORM models.

Importing this package registers every table on ``Base.metadata``, which
is what Alembic autogenerate needs to discover schema changes.
"""

from __future__ import annotations

from app.models.audit_event import AuditEvent, AuditEventType
from app.models.building import Building
from app.models.command_queue import (
    CommandBatch,
    CommandQueueEntry,
    CommandStatus,
)
from app.models.cumulative_delta import CumulativeDelta
from app.models.daily_seal import DailySeal
from app.models.effective_target import EffectiveTarget
from app.models.event_log import EventLogEntry, EventSeverity
from app.models.llm_call_log import LLMCallLog, LLMCallOutcome
from app.models.location import Location
from app.models.pending_approval import PendingApproval, PendingStatus
from app.models.recipe_revision import (
    RecipeRevision,
    RecipeRevisionParam,
    RecipeStatus,
)
from app.models.room import Room
from app.models.room_runtime import RoomRuntime
from app.models.runtime_adjustment import (
    AdjustmentMode,
    AdjustmentSource,
    RuntimeAdjustment,
)
from app.models.sensor_snapshot import SensorSnapshot
from app.models.telegram_user_map import TelegramUserMap
from app.models.user import Role, RoleName, User, UserRole

__all__ = [
    "AdjustmentMode",
    "AdjustmentSource",
    "AuditEvent",
    "AuditEventType",
    "Building",
    "CommandBatch",
    "CommandQueueEntry",
    "CommandStatus",
    "CumulativeDelta",
    "DailySeal",
    "EffectiveTarget",
    "EventLogEntry",
    "EventSeverity",
    "LLMCallLog",
    "LLMCallOutcome",
    "Location",
    "PendingApproval",
    "PendingStatus",
    "RecipeRevision",
    "RecipeRevisionParam",
    "RecipeStatus",
    "Role",
    "RoleName",
    "Room",
    "RoomRuntime",
    "RuntimeAdjustment",
    "SensorSnapshot",
    "TelegramUserMap",
    "User",
    "UserRole",
]
