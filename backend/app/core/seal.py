"""Daily seal computation + audit-chain verification.

Two responsibilities, both in service of plan locked decision #6
(tamper-evident HMAC chain + daily off-box seal):

* :func:`compute_daily_seal` — once per day, capture that day's chain
  span (first/last ``audit_event`` ids) and the chain head (the last
  row's HMAC), then sign a *seal HMAC* over
  ``(date, first_id, last_id, last_event_hmac)`` with the current key.
  The seal row is what gets exported off-box (see
  :mod:`app.workers.seal`). Idempotent per date.
* :func:`verify_chain` — walk the ``audit_event`` chain, recompute each
  row's HMAC server-side using the *exact* canonical payload the
  ``trg_audit_event_hmac_chain`` trigger uses, and confirm both the
  per-row HMAC and the ``prev_event_hash`` links. Any mismatch is a
  formal-deviation-grade finding.

The per-row recompute SQL is kept byte-identical to the trigger (and to
``tests/integration/test_audit_chain.py``). If the trigger's canonical
payload ever changes, :data:`_ROW_HMAC_SQL` must change with it.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import text

from app.config import get_settings
from app.models.daily_seal import DailySeal

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger(__name__)

# ASCII unit separator (0x1F) — the field delimiter the trigger uses
# between canonical-payload components. Must match the trigger exactly.
_FIELD_SEP = b"\x1f"


# Recompute one ``audit_event`` row's HMAC entirely in Postgres, using
# the same canonical byte layout as the BEFORE INSERT trigger:
#
#   prev_event_hash || key_id <US> epoch <US> actor_id <US>
#   event_type <US> room_id <US> params <US> reason_codes
#
# keyed by the per-row key from the ``audit.hmac_key_<id>`` GUC. This is
# the verifier's source of truth; keep it in lockstep with the trigger
# and with tests/integration/test_audit_chain.py.
_ROW_HMAC_SQL = text(
    r"""
    SELECT hmac(
        prev_event_hash
        || convert_to(key_id::text,                            'UTF8') || E'\037'::bytea
        || convert_to((extract(epoch from occurred_at))::text, 'UTF8') || E'\037'::bytea
        || convert_to(coalesce(actor_id, ''),                  'UTF8') || E'\037'::bytea
        || convert_to(event_type::text,                        'UTF8') || E'\037'::bytea
        || convert_to(coalesce(room_id, ''),                   'UTF8') || E'\037'::bytea
        || convert_to(params::text,                            'UTF8') || E'\037'::bytea
        || convert_to(reason_codes::text,                      'UTF8'),
        decode(current_setting('audit.hmac_key_' || key_id::text), 'hex'),
        'sha256'
    ) AS expected_hmac
    FROM audit_event WHERE id = :id
    """
)


@dataclass
class ChainVerifyResult:
    """Outcome of an audit-chain verification pass.

    Attributes:
        ok: ``True`` if every checked row's HMAC and ``prev_event_hash``
            link verified.
        rows_checked: Number of ``audit_event`` rows examined.
        first_bad_id: Id of the first row that failed, or ``None`` when
            the chain is intact.
        message: Human-readable summary suitable for an audit row /
            inspector export.
    """

    ok: bool
    rows_checked: int
    first_bad_id: int | None
    message: str


def _seal_payload(
    seal_date: dt.date,
    first_id: int,
    last_id: int,
    last_event_hmac: bytes,
) -> bytes:
    """Build the canonical byte payload signed into the seal HMAC.

    Layout mirrors the audit row trigger's style: ASCII-unit-separated
    text fields, then the raw chain-head HMAC bytes appended last.
    """
    return (
        seal_date.isoformat().encode("utf-8")
        + _FIELD_SEP
        + str(first_id).encode("utf-8")
        + _FIELD_SEP
        + str(last_id).encode("utf-8")
        + _FIELD_SEP
        + last_event_hmac
    )


def _compute_seal_hmac(
    key_hex: str,
    seal_date: dt.date,
    first_id: int,
    last_id: int,
    last_event_hmac: bytes,
) -> bytes:
    """HMAC-SHA256 the seal payload with the hex-encoded key."""
    return hmac.new(
        bytes.fromhex(key_hex),
        _seal_payload(seal_date, first_id, last_id, last_event_hmac),
        hashlib.sha256,
    ).digest()


async def compute_daily_seal(
    session: AsyncSession, seal_date: dt.date
) -> DailySeal:
    """Compute (or return the existing) :class:`DailySeal` for *seal_date*.

    Finds the first and last ``audit_event`` rows whose ``occurred_at``
    falls on *seal_date* (UTC), takes the last row's ``hmac`` as the
    chain head, and signs a seal HMAC over
    ``(date, first_id, last_id, last_event_hmac)`` with the current
    HMAC key. Inserts one ``daily_seal`` row.

    Idempotent: if a seal for *seal_date* already exists it is returned
    unchanged (the ``daily_seal.seal_date`` unique constraint also
    guards against races).

    Args:
        session: Active async session. The caller controls the
            transaction; this function only flushes.
        seal_date: The calendar date (UTC) to seal.

    Returns:
        The persisted (or pre-existing) :class:`DailySeal`.

    Raises:
        ValueError: If no audit events exist for *seal_date* — there is
            nothing to seal, and an empty seal would be misleading.
    """
    existing = await session.execute(
        text("SELECT id FROM daily_seal WHERE seal_date = :d"),
        {"d": seal_date},
    )
    existing_id = existing.scalar_one_or_none()
    if existing_id is not None:
        seal = await session.get(DailySeal, existing_id)
        assert seal is not None  # row id came straight from the DB
        log.info("daily_seal_exists", seal_date=seal_date.isoformat())
        return seal

    # UTC day bounds [start, next-day-start).
    day_start = dt.datetime.combine(seal_date, dt.time.min, tzinfo=dt.UTC)
    day_end = day_start + dt.timedelta(days=1)

    bounds = await session.execute(
        text(
            "SELECT min(id) AS first_id, max(id) AS last_id "
            "FROM audit_event "
            "WHERE occurred_at >= :start AND occurred_at < :end"
        ),
        {"start": day_start, "end": day_end},
    )
    first_id, last_id = bounds.one()
    if first_id is None or last_id is None:
        raise ValueError(f"no audit events on {seal_date.isoformat()}; nothing to seal")

    head = await session.execute(
        text("SELECT hmac, key_id FROM audit_event WHERE id = :id"),
        {"id": last_id},
    )
    last_event_hmac, _head_key_id = head.one()
    last_event_hmac = bytes(last_event_hmac)

    settings = get_settings()
    key_id = settings.hmac_key_id_current
    key_hex = settings.hmac_keys.get(key_id, "")
    if not key_hex:
        raise RuntimeError(f"HMAC key {key_id} not configured; cannot seal")

    seal_hmac = _compute_seal_hmac(
        key_hex, seal_date, int(first_id), int(last_id), last_event_hmac
    )

    seal = DailySeal(
        seal_date=seal_date,
        first_event_id=int(first_id),
        last_event_id=int(last_id),
        last_event_hmac=last_event_hmac,
        seal_hmac=seal_hmac,
        key_id=key_id,
    )
    session.add(seal)
    await session.flush()
    await session.refresh(seal)

    log.info(
        "daily_seal_computed",
        seal_date=seal_date.isoformat(),
        first_event_id=int(first_id),
        last_event_id=int(last_id),
        key_id=key_id,
    )
    return seal


def verify_seal(seal: DailySeal, key_hex: str) -> bool:
    """Recompute *seal*'s HMAC with *key_hex* and compare it constant-time.

    Args:
        seal: The :class:`DailySeal` row to check.
        key_hex: Hex-encoded HMAC key matching ``seal.key_id``.

    Returns:
        ``True`` if the recomputed seal HMAC matches the stored value.
    """
    recomputed = _compute_seal_hmac(
        key_hex,
        seal.seal_date,
        seal.first_event_id,
        seal.last_event_id,
        bytes(seal.last_event_hmac),
    )
    return hmac.compare_digest(recomputed, bytes(seal.seal_hmac))


async def verify_chain(
    session: AsyncSession,
    *,
    start_id: int | None = None,
    end_id: int | None = None,
) -> ChainVerifyResult:
    """Walk and verify the ``audit_event`` HMAC chain.

    For each row in the (optionally bounded) range, ordered by id:

    * recompute the per-row HMAC server-side with :data:`_ROW_HMAC_SQL`
      (the trigger's exact canonical payload) and compare it to the
      stored ``hmac``;
    * confirm ``prev_event_hash`` equals the previous row's ``hmac``
      (chain linkage).

    The walk stops at the first failure and reports its id.

    Args:
        session: Active async session with the relevant
            ``audit.hmac_key_<id>`` GUCs set (the standard ``session``
            fixture / :func:`app.db.get_session` does this).
        start_id: Inclusive lower bound on ``audit_event.id``; ``None``
            starts at the chain's genesis.
        end_id: Inclusive upper bound; ``None`` runs to the latest row.

    Returns:
        A :class:`ChainVerifyResult`. ``ok`` is ``True`` for an intact
        chain (including the trivially-intact empty range).
    """
    clauses: list[str] = []
    binds: dict[str, int] = {}
    if start_id is not None:
        clauses.append("id >= :start_id")
        binds["start_id"] = start_id
    if end_id is not None:
        clauses.append("id <= :end_id")
        binds["end_id"] = end_id
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    # ``where`` is assembled only from fixed internal literals above
    # ("id >= :start_id" / "id <= :end_id"); the actual bounds travel as
    # bound parameters in ``binds``. No user text reaches the SQL string.
    rows = (
        await session.execute(
            text(
                "SELECT id, prev_event_hash, hmac FROM audit_event "  # noqa: S608
                f"{where} ORDER BY id"
            ),
            binds,
        )
    ).all()

    if not rows:
        return ChainVerifyResult(
            ok=True, rows_checked=0, first_bad_id=None, message="empty range; nothing to verify"
        )

    prev_hmac: bytes | None = None
    checked = 0
    for row in rows:
        row_id = int(row[0])
        prev_event_hash = bytes(row[1])
        stored_hmac = bytes(row[2])

        # 1) Per-row HMAC must match the trigger's canonical recompute.
        recomputed = (
            await session.execute(_ROW_HMAC_SQL, {"id": row_id})
        ).scalar_one()
        if bytes(recomputed) != stored_hmac:
            return ChainVerifyResult(
                ok=False,
                rows_checked=checked,
                first_bad_id=row_id,
                message=f"HMAC mismatch at audit_event id={row_id} (row tampered or key wrong)",
            )

        # 2) Chain linkage: prev_event_hash links to the prior row's HMAC.
        # Only meaningful when the prior row is *inside the verified
        # range* — the first row of a partial range has a legitimate
        # predecessor we did not load, so skip the link check for it.
        if prev_hmac is not None and prev_event_hash != prev_hmac:
            return ChainVerifyResult(
                ok=False,
                rows_checked=checked,
                first_bad_id=row_id,
                message=f"broken chain link at audit_event id={row_id} (prev_event_hash mismatch)",
            )

        prev_hmac = stored_hmac
        checked += 1

    return ChainVerifyResult(
        ok=True,
        rows_checked=checked,
        first_bad_id=None,
        message=f"chain intact; {checked} row(s) verified",
    )
