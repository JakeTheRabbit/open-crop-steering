"""Read-only ORM mapping for the ``effective_target`` materialized view.

The view is defined in the baseline Alembic migration; this class is
just a SELECT surface. We tag it with ``info={"is_view": True}`` so
Alembic autogenerate knows to skip it.

To refresh the view after writing overlays:

.. code-block:: python

    from sqlalchemy import text
    await session.execute(
        text("REFRESH MATERIALIZED VIEW CONCURRENTLY effective_target")
    )
"""

from __future__ import annotations

from sqlalchemy import Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class EffectiveTarget(Base):
    __tablename__ = "effective_target"

    room_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    day_index: Mapped[int] = mapped_column(Integer, primary_key=True)
    param_name: Mapped[str] = mapped_column(String(64), primary_key=True)

    value: Mapped[float] = mapped_column(Float, nullable=False)
    tolerance: Mapped[float | None] = mapped_column(Float, nullable=True)
    unit: Mapped[str | None] = mapped_column(String(32), nullable=True)
    recipe_revision_id: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = ({"info": {"is_view": True}},)
