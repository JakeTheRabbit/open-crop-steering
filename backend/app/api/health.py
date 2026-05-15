"""Liveness + readiness endpoints."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_session

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    """Process is up. Does not touch dependencies."""
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """Process is ready to serve. Touches Postgres."""
    settings = get_settings()
    result = await session.execute(text("SELECT 1"))
    db_ok = result.scalar() == 1
    return {
        "status": "ready" if db_ok else "not_ready",
        "mode": settings.mode,
        "db": "ok" if db_ok else "fail",
    }
