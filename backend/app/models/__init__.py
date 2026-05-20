"""SQLAlchemy ORM models.

Importing this package registers every table on ``Base.metadata``, which
is what Alembic autogenerate needs to discover schema changes.
"""

from __future__ import annotations

from app.models.audit_event import AuditEvent, AuditEventType
from app.models.batch import Batch
from app.models.building import Building
from app.models.command_queue import (
    CommandBatch,
    CommandQueueEntry,
    CommandStatus,
)
from app.models.cumulative_delta import CumulativeDelta
from app.models.daily_seal import DailySeal
from app.models.effective_target import EffectiveTarget
from app.models.equipment import Equipment
from app.models.event_log import EventLogEntry, EventSeverity
from app.models.genetics import Genetics
from app.models.grow_recipe import GrowRecipe
from app.models.grow_recipe_day_override import GrowRecipeDayOverride
from app.models.llm_call_log import LLMCallLog, LLMCallOutcome
from app.models.location import Location
from app.models.pending_approval import PendingApproval, PendingStatus
from app.models.plant import Plant
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
from app.models.sensor import Sensor
from app.models.sensor_integration import SensorIntegration
from app.models.sensor_reading import SensorReading
from app.models.sensor_snapshot import SensorSnapshot
from app.models.telegram_user_map import TelegramUserMap
from app.models.user import Role, RoleName, User, UserRole

__all__ = [
    "AdjustmentMode",
    "AdjustmentSource",
    "AuditEvent",
    "AuditEventType",
    "Batch",
    "Building",
    "CommandBatch",
    "CommandQueueEntry",
    "CommandStatus",
    "CumulativeDelta",
    "DailySeal",
    "EffectiveTarget",
    "Equipment",
    "EventLogEntry",
    "EventSeverity",
    "Genetics",
    "GrowRecipe",
    "GrowRecipeDayOverride",
    "LLMCallLog",
    "LLMCallOutcome",
    "Location",
    "PendingApproval",
    "PendingStatus",
    "Plant",
    "RecipeRevision",
    "RecipeRevisionParam",
    "RecipeStatus",
    "Role",
    "RoleName",
    "Room",
    "RoomRuntime",
    "RuntimeAdjustment",
    "Sensor",
    "SensorIntegration",
    "SensorReading",
    "SensorSnapshot",
    "TelegramUserMap",
    "User",
    "UserRole",
]
