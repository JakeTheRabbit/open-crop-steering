"""Users + roles + user_roles.

Identity comes from Home Assistant (Ingress header
``X-Remote-User-Id``) in add-on mode and from JWT subject claims in
standalone mode. The ``id`` here is whatever HA hands us — typically a
UUID-like string.
"""

from __future__ import annotations

import datetime as dt
import enum

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    String,
    func,
)
from sqlalchemy import Enum as SqlEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class RoleName(enum.StrEnum):
    operator = "operator"
    cultivator = "cultivator"
    qap = "qap"
    admin = "admin"


class User(Base):
    """A person with access to the system. ``id`` is the HA user_id."""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(128))
    email: Mapped[str | None] = mapped_column(String(256), nullable=True)
    active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="true", nullable=False
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    role_assignments: Mapped[list[UserRole]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class Role(Base):
    """Static role catalogue. Seeded by baseline migration."""

    __tablename__ = "roles"

    name: Mapped[RoleName] = mapped_column(
        SqlEnum(RoleName, name="role_name", native_enum=True, create_type=False),
        primary_key=True,
    )
    description: Mapped[str | None] = mapped_column(String(256), nullable=True)


class UserRole(Base):
    """Join table; one user may hold multiple roles."""

    __tablename__ = "user_roles"

    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    role_name: Mapped[RoleName] = mapped_column(
        SqlEnum(RoleName, name="role_name", native_enum=True, create_type=False),
        ForeignKey("roles.name", ondelete="CASCADE"),
        primary_key=True,
    )
    granted_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    granted_by: Mapped[str | None] = mapped_column(String(64), nullable=True)

    user: Mapped[User] = relationship(back_populates="role_assignments")
