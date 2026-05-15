"""Daily seal — one HMAC over the day's chain head, exported off-box.

The ``worker-seal`` service materializes one of these rows per day at
midnight UTC, then ships it (encrypted) to the configured external
storage target. Inspectors can replay any range of seals against the
``audit_event`` chain to prove no rows were inserted, deleted, or
modified.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    Integer,
    LargeBinary,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class DailySeal(Base):
    __tablename__ = "daily_seal"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    seal_date: Mapped[dt.date] = mapped_column(
        Date, unique=True, index=True, nullable=False
    )

    first_event_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    last_event_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    last_event_hmac: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    seal_hmac: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    key_id: Mapped[int] = mapped_column(Integer, nullable=False)

    sealed_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    exported_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    export_target: Mapped[str | None] = mapped_column(String(512), nullable=True)
    export_locator: Mapped[str | None] = mapped_column(String(512), nullable=True)
