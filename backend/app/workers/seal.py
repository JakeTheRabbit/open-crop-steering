"""``worker-seal`` — daily audit seal + off-box export + cold backup.

Runs as its own s6 service (plan locked decision #3). Each day it:

1. Computes the previous day's :class:`DailySeal` (HMAC over the day's
   chain head — see :func:`app.core.seal.compute_daily_seal`).
2. Writes that day's audit rows + the seal record to a gzipped export
   file under ``/data/audit-seal-exports/<date>.sealed.gz``, then
   stamps ``daily_seal.exported_at`` / ``export_locator``.
3. (Wired later) ships the export to the configured off-box target
   and runs the ``pg_dump`` cold backup.

Coordination: the daily-seal step runs under the ``seal_daily``
Postgres advisory lock so a restarted/duplicated worker can't seal
twice (the ``daily_seal.seal_date`` unique constraint is the backstop).

ENCRYPTION — TODO (P13): the plan calls for *encrypted* off-box seal
exports. This module currently writes **gzip only, unencrypted**. Phase
13 (docs + packaging) wires age/GPG envelope encryption with a key
distinct from the HMAC key, plus the off-box copy (S3/B2). The export
filename suffix ``.sealed.gz`` becomes ``.sealed.gz.age`` then. Until
then, rely on HA backup encryption for at-rest protection of ``/data``.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import gzip
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import text

from app.config import get_settings
from app.core.locks import LockBusyError, advisory_lock
from app.core.seal import compute_daily_seal
from app.models.daily_seal import DailySeal

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)

#: Where sealed daily exports land inside the add-on's ``/data`` volume.
SEAL_EXPORT_DIR = Path("/data/audit-seal-exports")

#: Where ``pg_dump`` cold backups land inside ``/data``.
POSTGRES_BACKUP_DIR = Path("/data/postgres-backups")

#: Advisory-lock name guarding the once-per-day seal computation.
SEAL_LOCK = "seal_daily"


def _yesterday(today: dt.date | None = None) -> dt.date:
    """Return the date to seal — the day before *today* (UTC)."""
    ref = today or dt.datetime.now(dt.UTC).date()
    return ref - dt.timedelta(days=1)


async def _audit_rows_for_day(
    session: AsyncSession, seal_date: dt.date
) -> list[dict[str, Any]]:
    """Return every ``audit_event`` row for *seal_date* as JSON-able dicts.

    Binary chain fields (``prev_event_hash`` / ``hmac``) are hex-encoded
    so the export is plain JSON.
    """
    day_start = dt.datetime.combine(seal_date, dt.time.min, tzinfo=dt.UTC)
    day_end = day_start + dt.timedelta(days=1)
    result = await session.execute(
        text(
            "SELECT id, occurred_at, event_type, actor_id, actor_role, "
            "room_id, params, reason_codes, summary, key_id, "
            "encode(prev_event_hash, 'hex') AS prev_event_hash, "
            "encode(hmac, 'hex') AS hmac "
            "FROM audit_event "
            "WHERE occurred_at >= :start AND occurred_at < :end "
            "ORDER BY id"
        ),
        {"start": day_start, "end": day_end},
    )
    rows: list[dict[str, Any]] = []
    for m in result.mappings():
        row = dict(m)
        occurred = row.get("occurred_at")
        if isinstance(occurred, dt.datetime):
            row["occurred_at"] = occurred.isoformat()
        rows.append(row)
    return rows


def _write_export(
    seal_date: dt.date,
    seal: DailySeal,
    audit_rows: list[dict[str, Any]],
    export_dir: Path = SEAL_EXPORT_DIR,
) -> Path:
    """Write the gzipped ``<date>.sealed.gz`` export and return its path.

    The payload bundles the seal record (hex-encoded HMAC fields) with
    the day's audit rows. See the module docstring for the encryption
    TODO — this is currently gzip-only.
    """
    export_dir.mkdir(parents=True, exist_ok=True)
    target = export_dir / f"{seal_date.isoformat()}.sealed.gz"
    payload = {
        "schema": "ocs.audit_seal_export.v1",
        "seal_date": seal_date.isoformat(),
        "exported_at": dt.datetime.now(dt.UTC).isoformat(),
        "seal": {
            "first_event_id": seal.first_event_id,
            "last_event_id": seal.last_event_id,
            "last_event_hmac": bytes(seal.last_event_hmac).hex(),
            "seal_hmac": bytes(seal.seal_hmac).hex(),
            "key_id": seal.key_id,
        },
        "audit_events": audit_rows,
        "_encryption": "none (gzip only) — see worker docstring, encrypted in P13",
    }
    with gzip.open(target, "wt", encoding="utf-8") as fh:
        json.dump(payload, fh, separators=(",", ":"))
    return target


class SealWorker:
    """The daily-seal worker loop.

    Args:
        session_factory: An ``async_sessionmaker`` producing sessions
            with the ``audit.hmac_key_<id>`` GUCs set (the standard
            factory from :func:`app.db.get_session_factory`).
        export_dir: Override for the seal export directory (tests).
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        export_dir: Path = SEAL_EXPORT_DIR,
    ) -> None:
        self._session_factory = session_factory
        self._export_dir = export_dir

    async def run_once(self, seal_date: dt.date | None = None) -> DailySeal | None:
        """Seal one day, write its export, and stamp the seal row.

        Held under the ``seal_daily`` advisory lock so a duplicated
        worker process is a no-op rather than a double-seal.

        Args:
            seal_date: Date to seal; defaults to yesterday (UTC).

        Returns:
            The :class:`DailySeal` produced (or pre-existing), or
            ``None`` if another process holds the lock or the day had
            no audit events.
        """
        target_date = seal_date or _yesterday()
        async with self._session_factory() as session:
            try:
                async with advisory_lock(session, SEAL_LOCK):
                    return await self._seal_and_export(session, target_date)
            except LockBusyError:
                log.info("seal_lock_busy", seal_date=target_date.isoformat())
                return None

    async def _seal_and_export(
        self, session: AsyncSession, seal_date: dt.date
    ) -> DailySeal | None:
        """Compute the seal, write the export, stamp + commit."""
        try:
            seal = await compute_daily_seal(session, seal_date)
        except ValueError:
            # No audit events that day — nothing to seal.
            log.info("seal_skipped_no_events", seal_date=seal_date.isoformat())
            return None

        already_exported = seal.exported_at is not None
        audit_rows = await _audit_rows_for_day(session, seal_date)
        export_path = await asyncio.to_thread(
            _write_export, seal_date, seal, audit_rows, self._export_dir
        )

        if not already_exported:
            seal.exported_at = dt.datetime.now(dt.UTC)
            seal.export_target = get_settings().seal_export_target or None
            seal.export_locator = str(export_path)
        await session.commit()

        log.info(
            "daily_seal_exported",
            seal_date=seal_date.isoformat(),
            export_path=str(export_path),
            audit_rows=len(audit_rows),
        )
        return seal

    async def run_forever(
        self,
        *,
        interval: dt.timedelta = dt.timedelta(days=1),
        sleeper: Callable[[float], Any] | None = None,
    ) -> None:  # pragma: no cover - long-running loop driven by s6
        """Run :meth:`run_once` forever, once per *interval*.

        Args:
            interval: Gap between seal passes (daily in production).
            sleeper: Async sleep function; defaults to
                :func:`asyncio.sleep`. Injectable for tests.
        """
        sleep = sleeper or asyncio.sleep
        log.info("seal_worker_started", interval_seconds=interval.total_seconds())
        while True:
            try:
                await self.run_once()
            except Exception:
                log.exception("seal_worker_iteration_failed")
            await sleep(interval.total_seconds())


async def cold_backup(
    target_dir: Path = POSTGRES_BACKUP_DIR,
    *,
    database_url: str | None = None,
) -> Path:
    """Run a ``pg_dump`` directory-format cold backup of the database.

    Shells out to ``pg_dump --format=directory --jobs=4`` (plan locked
    decision #20). Intended to be invoked by the worker on a schedule
    and by the pre-HA-backup hook the s6 packaging wires later
    (Phase 11) — that hook is *not* wired here.

    Args:
        target_dir: Parent directory for the dump; a timestamped
            sub-directory is created inside it.
        database_url: Override connection URL; defaults to the app's
            ``DATABASE_URL``. The ``+asyncpg`` driver suffix is stripped
            because ``pg_dump`` wants a plain libpq URL.

    Returns:
        Path to the dump directory.

    Raises:
        RuntimeError: If ``pg_dump`` exits non-zero.
    """
    await asyncio.to_thread(target_dir.mkdir, parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    dump_dir = target_dir / f"pgdump-{stamp}"

    url = database_url or get_settings().database_url
    # pg_dump speaks libpq, not the SQLAlchemy async driver dialect.
    for suffix in ("+asyncpg", "+psycopg2", "+psycopg"):
        url = url.replace(suffix, "")

    proc = await asyncio.create_subprocess_exec(
        "pg_dump",
        "--format=directory",
        "--jobs=4",
        "--no-owner",
        "--no-privileges",
        f"--file={dump_dir}",
        url,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        msg = stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"pg_dump failed (exit {proc.returncode}): {msg}")

    log.info("cold_backup_complete", dump_dir=str(dump_dir))
    return dump_dir
