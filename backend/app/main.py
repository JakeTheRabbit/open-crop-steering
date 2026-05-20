"""FastAPI application entrypoint.

Beyond wiring the API routers, this module mounts the static Next.js
export (the planner UI) when it is present, so the add-on serves the
whole UI under Home Assistant Ingress from the same process as the API.

The static export is *optional*: in a dev / test checkout there is no
``frontend/out/`` build, so the mount is skipped without erroring. In
the packaged add-on the Dockerfile copies the export to
``/opt/frontend`` and sets ``OCS_FRONTEND_DIR`` to point at it.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import structlog
from fastapi import FastAPI

from app.api import (
    admin,
    approvals,
    audit,
    config_wizard,
    cultivation,
    equipment,
    health,
    knowledge,
    rollout,
    rooms,
    sensors,
    sites,
)
from app.config import get_settings
from app.db import dispose_engine, get_engine
from app.logging_config import configure_logging

log = structlog.get_logger(__name__)


def _candidate_frontend_dirs() -> list[Path]:
    """Return the directories to probe for the static Next.js export.

    Checked in order:

    1. ``$OCS_FRONTEND_DIR`` — set by the add-on Dockerfile to the
       baked-in export location (``/opt/frontend``).
    2. ``frontend/out`` relative to the repo root (two levels above this
       package's ``backend/`` dir) — a local ``npm run build`` checkout.

    Returns:
        Existing-or-not candidate paths, highest priority first.
    """
    candidates: list[Path] = []
    env_dir = os.getenv("OCS_FRONTEND_DIR")
    if env_dir:
        candidates.append(Path(env_dir))
    repo_root = Path(__file__).resolve().parents[2]
    candidates.append(repo_root / "frontend" / "out")
    return candidates


def _resolve_frontend_dir() -> Path | None:
    """Return the first existing static-export dir, or ``None``.

    ``None`` means no build is present — the SPA mount is skipped and the
    app serves the API only (dev / test posture).
    """
    for candidate in _candidate_frontend_dirs():
        if candidate.is_dir() and (candidate / "index.html").is_file():
            return candidate
    return None


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
app.include_router(approvals.router)
app.include_router(knowledge.router)
app.include_router(rollout.router)
app.include_router(config_wizard.router)
app.include_router(rooms.router)
# Convex-aligned CRUD surfaces (pass 5-backend) — sites / sensors /
# equipment / cultivation. The new ``sites`` router exposes the
# Convex-aligned ``rooms`` table at /api/sites/rooms; the legacy
# room_runtime API above stays at /api/rooms.
app.include_router(sites.router)
app.include_router(sensors.sensors_router)
app.include_router(sensors.readings_router)
app.include_router(sensors.integrations_router)
app.include_router(equipment.router)
app.include_router(cultivation.router)


def _mount_static_ui(application: FastAPI) -> None:
    """Mount the static Next.js export at ``/`` if a build is present.

    The Next.js export is served SPA-style: a request that does not match
    an API route or a real static file falls through to ``index.html``,
    so client-side routing (``/planner/[room]`` etc.) works under Ingress.

    This is wired *after* the API routers are included, so ``/api/*``,
    ``/healthz`` and ``/readyz`` always win — the catch-all only sees a
    path no router claimed. If no build directory exists (dev / test),
    the mount is skipped entirely and the app serves the API only.

    Args:
        application: The FastAPI app to mount onto.
    """
    frontend_dir = _resolve_frontend_dir()
    if frontend_dir is None:
        log.info("static_ui_skip", reason="no frontend build directory found")
        return

    # Imported here so a checkout without the export still imports main
    # cleanly even if starlette internals shift.
    from starlette.staticfiles import StaticFiles  # noqa: PLC0415

    class _SpaStaticFiles(StaticFiles):
        """``StaticFiles`` for the Next.js export, with three fix-ups.

        1. Asset-path normalization. The export references its assets
           *relative* to the page (``./_next/...``), so a sub-route load
           (``/admin/rooms/``) asks the server for
           ``admin/rooms/_next/static/...``. Any path containing
           ``_next/`` is rewritten to the real export-root location so
           CSS/JS resolve from every route, in both standalone and
           Ingress deployments.
        2. SPA fallback. A static export has no server router; an
           unmatched non-asset path is a client-side route, so the SPA
           shell (``index.html``) is served instead of a 404.
        3. Cache policy. ``_next/static/*`` is content-hashed, so it is
           served ``immutable`` for a year; the HTML shell (and any
           other non-hashed file) is served ``no-cache`` so a browser
           revalidates and picks up a rebuilt bundle instead of serving
           a stale one.
        """

        async def get_response(self, path: str, scope: object):  # type: ignore[no-untyped-def]
            from starlette.exceptions import (  # noqa: PLC0415
                HTTPException as StarletteHTTPException,
            )

            # (1) strip any sub-route prefix in front of a "_next/" asset
            marker = "_next/"
            idx = path.find(marker)
            if idx > 0:
                path = path[idx:]

            try:
                response = await super().get_response(path, scope)  # type: ignore[arg-type]
            except StarletteHTTPException as exc:
                if exc.status_code != 404:  # noqa: PLR2004 — only 404 -> SPA shell
                    raise
                # (2) a genuine miss that is NOT an asset → SPA shell
                if marker in path:
                    raise
                response = await super().get_response("index.html", scope)  # type: ignore[arg-type]
                path = "index.html"

            # (3) cache policy — content-hashed assets are immutable;
            # the HTML shell must revalidate so a rebuilt bundle wins
            # over a browser-cached stale copy.
            if path.startswith(marker):
                response.headers["Cache-Control"] = (
                    "public, max-age=31536000, immutable"
                )
            else:
                response.headers["Cache-Control"] = "no-cache"
            return response

    application.mount(
        "/",
        _SpaStaticFiles(directory=str(frontend_dir), html=True),
        name="ui",
    )
    log.info("static_ui_mounted", directory=str(frontend_dir))


# Mount last so the SPA catch-all never shadows an API route.
_mount_static_ui(app)
