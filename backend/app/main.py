"""FastAPI application entrypoint."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI

from app.api import admin, audit, health
from app.config import get_settings
from app.db import dispose_engine, get_engine
from app.logging_config import configure_logging

log = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    settings = get_settings()
    log.info(
        "startup",
        mode=settings.mode,
        ha_url=settings.ha_url,
        api_port=settings.api_port,
        hmac_key_ids=sorted(settings.hmac_keys.keys()),
        hmac_key_id_current=settings.hmac_key_id_current,
    )
    # Touch the engine so a bad DATABASE_URL fails immediately on boot
    get_engine()
    yield
    log.info("shutdown")
    await dispose_engine()


app = FastAPI(
    title="Open Crop Steering",
    version="0.1.0",
    description=(
        "AI-supervised cannabis cultivation control plane. "
        "See https://github.com/JakeTheRabbit/open-crop-steering."
    ),
    lifespan=lifespan,
)
app.include_router(health.router)
app.include_router(audit.router)
app.include_router(admin.router)
