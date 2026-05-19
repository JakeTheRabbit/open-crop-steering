"""Process entrypoints for every s6 service (Phase 11/12 packaging).

The add-on (and the standalone image) runs a single Docker container with
six s6-overlay services. They all start the *same* image; each service's
``run`` script invokes one subcommand of this module:

* ``serve`` — the ``api`` service: runs the FastAPI app under uvicorn.
* ``worker <name>`` — one of the four worker services
  (``executor`` / ``supervisor`` / ``alerts`` / ``seal``): builds the
  named worker with its real dependencies and runs its ``run_forever``.
* ``migrate`` — runs ``alembic upgrade head``; the ``api`` service runs
  this once before ``serve`` so the workers (sequenced after ``api`` by
  s6 ``dependencies``) always see an up-to-date schema.
* ``cold-backup`` — runs :func:`app.workers.seal.cold_backup`; wired as
  the pre-HA-backup hook so ``/data`` is captured with a consistent
  ``pg_dump`` rather than a live Postgres data directory.

Dependency wiring lives in one place — :func:`build_worker` — so the s6
``run`` scripts stay trivial (``exec python -m app.cli worker <name>``)
and the dependency graph is testable without a container. The factory
map :data:`WORKER_FACTORIES` is what the smoke test asserts against.

``argparse`` only — ``click`` is deliberately not a dependency.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

import structlog

from app.config import get_settings
from app.db import get_session_factory, open_session
from app.logging_config import configure_logging

log = structlog.get_logger(__name__)

#: The four worker names the ``worker`` subcommand accepts. Order matches
#: the s6 service directories under ``rootfs/etc/services.d``.
WORKER_NAMES = ("executor", "supervisor", "alerts", "seal")


# ---------------------------------------------------------------------------
# Worker dependency wiring.
# ---------------------------------------------------------------------------


def _build_executor() -> tuple[object, Callable[[object], Awaitable[None]]]:
    """Build the :class:`~app.workers.executor.Executor` + its run coroutine.

    The executor needs the GUC-setting :func:`app.db.open_session` factory
    and an :class:`~app.ha_client.HAClient` (mode-resolved token comes
    from :mod:`app.config`). Its loop poll interval is
    ``settings.executor_poll_seconds``.
    """
    from app.ha_client import HAClient  # noqa: PLC0415
    from app.workers.executor import Executor  # noqa: PLC0415

    settings = get_settings()
    worker = Executor(session_factory=open_session, ha_client=HAClient())

    async def _run(w: object) -> None:
        assert isinstance(w, Executor)
        await w.run_forever(poll_interval=float(settings.executor_poll_seconds))

    return worker, _run


def _build_supervisor() -> tuple[object, Callable[[object], Awaitable[None]]]:
    """Build the :class:`~app.workers.supervisor.Supervisor` + run coroutine.

    The supervisor needs the GUC-setting session factory plus an Influx,
    HA and LLM client. ``no_touch_windows`` is built from
    ``settings.no_touch_windows`` — each mapping is parsed (and validated)
    by :meth:`~app.core.no_touch.NoTouchWindow.from_mapping` here, at
    worker start, so a bad window fails loudly rather than silently at
    tick time. ``rooms_provider`` returns an empty list here: room
    configuration is operator-supplied at runtime (Phase 11 wizard /
    Phase 14 deploy) and is not known at process start, so v0.1 ticks
    over an empty room set until the room store is wired. The tick loop
    is otherwise fully live.
    """
    from app.core.no_touch import NoTouchWindow  # noqa: PLC0415
    from app.ha_client import HAClient  # noqa: PLC0415
    from app.influx_client import InfluxClient  # noqa: PLC0415
    from app.llm_client import LLMClient  # noqa: PLC0415
    from app.workers.supervisor import RoomTickInput, Supervisor  # noqa: PLC0415

    settings = get_settings()
    no_touch_windows = [
        NoTouchWindow.from_mapping(window)
        for window in settings.no_touch_windows
    ]

    def _rooms_provider() -> list[RoomTickInput]:
        # No persisted room store yet — the supervisor ticks an empty
        # set until rooms are configured. Documented in the docstring.
        return []

    worker = Supervisor(
        session_factory=open_session,
        influx=InfluxClient(),
        ha=HAClient(),
        llm=LLMClient(),
        rooms_provider=_rooms_provider,
        no_touch_windows=no_touch_windows,
    )

    async def _run(w: object) -> None:
        assert isinstance(w, Supervisor)
        await w.run_forever(interval=float(settings.supervisor_tick_seconds))

    return worker, _run


def _build_alerts() -> tuple[object, Callable[[object], Awaitable[None]]]:
    """Build the :class:`~app.workers.alerts.AlertsWorker` + run coroutine.

    Picks :class:`~app.workers.alerts.TelegramNotifier` when a Telegram
    bot token *and* chat id are both configured, else
    :class:`~app.workers.alerts.NullNotifier` (dashboard-only alerts).
    The worker has no ``run_forever`` of its own — its dispatch loop is
    driven here.
    """
    from app.workers.alerts import (  # noqa: PLC0415
        AlertsWorker,
        NullNotifier,
        TelegramNotifier,
    )

    settings = get_settings()
    notifier: object
    if settings.telegram_bot_token and settings.telegram_chat_id:
        notifier = TelegramNotifier(
            settings.telegram_bot_token, settings.telegram_chat_id
        )
        log.info("alerts_notifier_selected", notifier="telegram")
    else:
        notifier = NullNotifier()
        log.info("alerts_notifier_selected", notifier="null")
    worker = AlertsWorker(session_factory=open_session, notifier=notifier)  # type: ignore[arg-type]

    async def _run(w: object) -> None:
        assert isinstance(w, AlertsWorker)
        await _run_alerts_loop(w, poll_interval=30.0)

    return worker, _run


async def _run_alerts_loop(
    worker: object, *, poll_interval: float
) -> None:  # pragma: no cover - long-running loop driven by s6
    """Drive :meth:`AlertsWorker.run_once` on a fixed interval.

    :class:`~app.workers.alerts.AlertsWorker` exposes ``run_once`` (one
    dispatch scan) but no loop of its own, so the s6 service loops it
    here. Cancellation-safe.
    """
    from app.workers.alerts import AlertsWorker  # noqa: PLC0415

    assert isinstance(worker, AlertsWorker)
    log.info("alerts_loop_started", poll_interval=poll_interval)
    try:
        while True:
            try:
                await worker.run_once()
            except Exception:  # one scan failing must not kill the loop
                log.exception("alerts_run_once_error")
            await asyncio.sleep(poll_interval)
    except asyncio.CancelledError:
        log.info("alerts_loop_cancelled")
        raise


def _build_seal() -> tuple[object, Callable[[object], Awaitable[None]]]:
    """Build the :class:`~app.workers.seal.SealWorker` + run coroutine.

    The seal worker wants the *async_sessionmaker* (not the
    ``open_session`` context-manager factory) so it can open sessions
    with the audit GUCs set — :func:`app.db.get_session_factory` returns
    exactly that.
    """
    from app.workers.seal import SealWorker  # noqa: PLC0415

    worker = SealWorker(session_factory=get_session_factory())

    async def _run(w: object) -> None:
        assert isinstance(w, SealWorker)
        await w.run_forever()

    return worker, _run


#: Maps a worker name to its zero-arg builder. Each builder returns
#: ``(worker_instance, run_coroutine)`` — the run coroutine takes the
#: worker and awaits its loop. The :func:`build_worker` indirection keeps
#: the s6 ``run`` scripts trivial and makes the wiring unit-testable.
WORKER_FACTORIES: dict[
    str, Callable[[], tuple[object, Callable[[object], Awaitable[None]]]]
] = {
    "executor": _build_executor,
    "supervisor": _build_supervisor,
    "alerts": _build_alerts,
    "seal": _build_seal,
}


def build_worker(
    name: str,
) -> tuple[object, Callable[[object], Awaitable[None]]]:
    """Build the named worker with its real dependencies.

    Args:
        name: One of :data:`WORKER_NAMES`.

    Returns:
        ``(worker, run)`` — the worker instance and a coroutine that runs
        its loop when awaited with the worker.

    Raises:
        KeyError: If ``name`` is not a known worker.
    """
    factory = WORKER_FACTORIES[name]
    return factory()


# ---------------------------------------------------------------------------
# Subcommand implementations.
# ---------------------------------------------------------------------------


def _cmd_serve(_args: argparse.Namespace) -> int:
    """Run the FastAPI app under uvicorn — the ``api`` s6 service.

    Binds to ``settings.api_host`` / ``settings.api_port`` (the add-on's
    ``ingress_port`` 8099 by default). ``app.main:app`` is passed as an
    import string so uvicorn owns the process.
    """
    import uvicorn  # noqa: PLC0415

    settings = get_settings()
    log.info(
        "cli_serve",
        host=settings.api_host,
        port=settings.api_port,
        mode=settings.mode,
    )
    uvicorn.run(
        "app.main:app",
        host=settings.api_host,
        port=settings.api_port,
        log_config=None,  # structlog already configured stdout logging
    )
    return 0


async def _idle_forever() -> None:  # pragma: no cover - signal-driven
    """Block until the process is signalled (observe-only executor)."""
    await asyncio.Event().wait()


def _cmd_worker(args: argparse.Namespace) -> int:
    """Run one named worker loop — a worker s6 service.

    Builds the worker via :func:`build_worker` and runs its loop until
    cancelled (s6 sends SIGTERM on shutdown).

    Observe-only guard: when ``OCS_OBSERVE_ONLY`` is set the ``executor``
    worker is NOT built or run — it idles instead. The executor is the
    only component that calls HA ``call_service``, so not running it is
    a hard guarantee that the deployment writes nothing to Home
    Assistant. The other workers (supervisor / alerts / seal) are
    unaffected — they observe and report but never actuate.
    """
    name: str = args.name

    if name == "executor" and get_settings().ocs_observe_only:
        log.warning(
            "cli_worker_observe_only",
            worker=name,
            detail=(
                "OCS_OBSERVE_ONLY is set — executor disabled; the command "
                "queue will not be consumed and nothing is written to HA"
            ),
        )
        with contextlib.suppress(KeyboardInterrupt):  # pragma: no cover
            asyncio.run(_idle_forever())
        return 0

    log.info("cli_worker_start", worker=name)
    worker, run = build_worker(name)
    try:
        asyncio.run(run(worker))
    except KeyboardInterrupt:  # pragma: no cover - signal-driven
        log.info("cli_worker_interrupted", worker=name)
    return 0


def _cmd_migrate(_args: argparse.Namespace) -> int:
    """Run ``alembic upgrade head`` — invoked by the ``api`` service first.

    The Alembic config (``alembic.ini`` + the ``alembic`` script
    directory) is NOT part of the installed wheel, so its location is
    resolved in two ways:

    * ``OCS_ALEMBIC_DIR`` — set by the Docker image, which copies
      ``alembic.ini`` + ``alembic/`` to a fixed path (the wheel in
      ``site-packages`` has no sibling ``alembic.ini``).
    * otherwise the dev layout — ``backend/`` next to the ``app``
      package (editable install).

    The runtime ``sqlalchemy.url`` is taken from
    ``settings.database_url`` (the ini's placeholder URL is overridden
    here so no env-interpolation in the ini is needed).
    """
    import os  # noqa: PLC0415

    from alembic import command  # noqa: PLC0415
    from alembic.config import Config as AlembicConfig  # noqa: PLC0415

    alembic_dir_env = os.getenv("OCS_ALEMBIC_DIR")
    base = (
        Path(alembic_dir_env)
        if alembic_dir_env
        else Path(__file__).resolve().parent.parent
    )
    cfg = AlembicConfig(str(base / "alembic.ini"))
    cfg.set_main_option("script_location", str(base / "alembic"))
    cfg.set_main_option("sqlalchemy.url", get_settings().database_url)
    log.info("cli_migrate_start", alembic_dir=str(base))
    command.upgrade(cfg, "head")
    log.info("cli_migrate_complete")
    return 0


def _cmd_cold_backup(_args: argparse.Namespace) -> int:
    """Run a ``pg_dump`` cold backup — the pre-HA-backup hook.

    Invokes :func:`app.workers.seal.cold_backup`, which shells out to
    ``pg_dump --format=directory --jobs=4`` into ``/data/postgres-backups``.
    Home Assistant runs this immediately before snapshotting the add-on
    so the backup captures a consistent dump, not a live data directory.
    """
    from app.workers.seal import cold_backup  # noqa: PLC0415

    log.info("cli_cold_backup_start")
    dump_dir = asyncio.run(cold_backup())
    log.info("cli_cold_backup_complete", dump_dir=str(dump_dir))
    return 0


# ---------------------------------------------------------------------------
# Argument parser.
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Build the ``app.cli`` argument parser.

    Exposed (and kept side-effect free) so the smoke test can parse each
    subcommand without running it.

    Returns:
        The configured :class:`argparse.ArgumentParser`. Every subparser
        sets a ``func`` default — the handler :func:`main` dispatches to.
    """
    parser = argparse.ArgumentParser(
        prog="app.cli",
        description="Open Crop Steering process entrypoints (s6 services).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve = subparsers.add_parser(
        "serve", help="Run the FastAPI app (uvicorn) — the api service."
    )
    serve.set_defaults(func=_cmd_serve)

    worker = subparsers.add_parser(
        "worker", help="Run a named worker loop (executor/supervisor/alerts/seal)."
    )
    worker.add_argument(
        "name",
        choices=WORKER_NAMES,
        help="Which worker loop to run.",
    )
    worker.set_defaults(func=_cmd_worker)

    migrate = subparsers.add_parser(
        "migrate", help="Run 'alembic upgrade head'."
    )
    migrate.set_defaults(func=_cmd_migrate)

    cold_backup = subparsers.add_parser(
        "cold-backup",
        help="Run a pg_dump cold backup (pre-HA-backup hook).",
    )
    cold_backup.set_defaults(func=_cmd_cold_backup)

    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse ``argv`` and dispatch to the selected subcommand.

    Args:
        argv: Argument vector; defaults to ``sys.argv[1:]``.

    Returns:
        The subcommand's process exit code.
    """
    configure_logging()
    parser = build_parser()
    args = parser.parse_args(argv)
    handler: Callable[[argparse.Namespace], int] = args.func
    return handler(args)


if __name__ == "__main__":  # pragma: no cover - module entrypoint
    sys.exit(main())
