"""Daily seal computation + audit-chain verification.

Two responsibilities, both in service of plan locked decision #6
(tamper-evident HMAC chain + daily off-box seal):

* :func:`compute_daily_seal` — once per day, capture that day's id
  span and chain-tip HMAC (the last row in *chain* order, which need
  not be the ``MAX(id)`` row), then sign a *seal HMAC* over
  ``(date, first_id, last_id, last_event_hmac)`` with the current key.
  The seal row is what gets exported off-box (see
  :mod:`app.workers.seal`). Idempotent per date.
* :func:`verify_chain` — walk the ``audit_event`` chain by following
  ``prev_event_hash`` links (not ``id`` order — see the function
  docstring), recompute each row's HMAC server-side using the *exact*
  canonical payload the ``trg_audit_event_hmac_chain`` trigger uses,
  and confirm both the per-row HMAC and the chain structure. Any
  mismatch is a formal-deviation-grade finding.

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

# The genesis row's prev_event_hash: 32 zero bytes (the trigger seeds the
# first-ever row with decode(repeat('0',64),'hex')). Every other row's
# prev_event_hash is a real predecessor HMAC.
_GENESIS_PREV_HASH = b"\x00" * 32


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

    Records the day's id span (``first_event_id`` is the lowest id of
    the day) and the day's chain *tip* — the last row in chain order,
    the one no other row in the day links back to. The tip is found by
    ``prev_event_hash`` linkage, not ``MAX(id)``: ``id`` is assigned
    before the chain advisory lock, so concurrent inserts can leave id
    order out of step with chain order and ``MAX(id)`` may be a
    mid-chain row. Signs a seal HMAC over
    ``(date, first_id, last_id, last_event_hmac)`` with the current
    HMAC key and inserts one ``daily_seal`` row.

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
        RuntimeError: If the HMAC key is unconfigured, or the day's rows
            have no chain tip (they form a cycle — chain corrupt).
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

    # ``first_event_id`` is the day's lowest id — a stable lower bound
    # for the export's id span.
    first_id = (
        await session.execute(
            text(
                "SELECT min(id) FROM audit_event "
                "WHERE occurred_at >= :start AND occurred_at < :end"
            ),
            {"start": day_start, "end": day_end},
        )
    ).scalar_one()
    if first_id is None:
        raise ValueError(f"no audit events on {seal_date.isoformat()}; nothing to seal")

    # The chain head is the day's last row *in chain order* — the row no
    # other row in the day links back to. id order need not match chain
    # order (ids are issued before the chain advisory lock), so MAX(id)
    # can be a mid-chain row rather than the tip.
    tip_row = (
        await session.execute(
            text(
                "SELECT a.id, a.hmac FROM audit_event a "
                "WHERE a.occurred_at >= :start AND a.occurred_at < :end "
                "  AND NOT EXISTS ("
                "    SELECT 1 FROM audit_event b "
                "    WHERE b.occurred_at >= :start AND b.occurred_at < :end "
                "      AND b.prev_event_hash = a.hmac"
                "  ) "
                "ORDER BY a.occurred_at DESC, a.id DESC LIMIT 1"
            ),
            {"start": day_start, "end": day_end},
        )
    ).first()
    if tip_row is None:
        raise RuntimeError(
            f"audit events exist on {seal_date.isoformat()} but none is a "
            "chain tip — the day's rows form a cycle (chain corrupt)"
        )
    last_id = int(tip_row[0])
    last_event_hmac = bytes(tip_row[1])

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


def _verify_chain_linkage(
    loaded: list[tuple[int, bytes, bytes]],
    *,
    full_range: bool,
) -> ChainVerifyResult:
    """Check that *loaded* rows form a valid chain by ``prev_event_hash``.

    Pure / no I/O. *loaded* is ``(id, prev_event_hash, hmac)`` tuples in
    id order. Per-row HMACs are assumed already recomputed-and-matched
    by the caller; this verifies only the chain *structure*, following
    linkage pointers — never ``id`` order.

    For a full table the rows must form exactly one linked list rooted
    at the single genesis row. For a bounded range, rows whose
    predecessor falls outside the range are accepted as fragment starts.
    """
    total = len(loaded)

    # Index by hmac (to find a row) and by prev_event_hash (to find a
    # row's successor). A duplicate in either is structurally impossible
    # for an intact chain: equal hmac == two identical rows; equal
    # prev_event_hash == two rows chained onto one predecessor (a fork).
    by_hmac: dict[bytes, int] = {}
    by_prev: dict[bytes, int] = {}
    for row_id, prev, h in loaded:
        if h in by_hmac:
            return ChainVerifyResult(
                ok=False,
                rows_checked=total,
                first_bad_id=max(row_id, by_hmac[h]),
                message=(
                    f"duplicate HMAC at audit_event id={by_hmac[h]} and "
                    f"id={row_id} (chain tampered)"
                ),
            )
        if prev in by_prev:
            return ChainVerifyResult(
                ok=False,
                rows_checked=total,
                first_bad_id=max(row_id, by_prev[prev]),
                message=(
                    f"forked chain: audit_event id={by_prev[prev]} and "
                    f"id={row_id} share a prev_event_hash (chain tampered)"
                ),
            )
        by_hmac[h] = row_id
        by_prev[prev] = row_id

    # Fragment starts: rows whose predecessor is not itself loaded.
    starts: list[tuple[int, bytes]] = [
        (row_id, prev) for row_id, prev, _h in loaded if prev not in by_hmac
    ]
    if full_range:
        # The whole table — every start must be THE genesis row.
        non_genesis = sorted(
            row_id for row_id, prev in starts if prev != _GENESIS_PREV_HASH
        )
        if non_genesis:
            return ChainVerifyResult(
                ok=False,
                rows_checked=total,
                first_bad_id=non_genesis[0],
                message=(
                    f"broken chain link at audit_event id={non_genesis[0]}: "
                    "prev_event_hash matches no known row (predecessor "
                    "deleted, or row inserted out of band)"
                ),
            )
        # Every remaining start carries the all-zero genesis hash.
        if len(starts) != 1:
            bad = min((row_id for row_id, _p in starts), default=loaded[0][0])
            return ChainVerifyResult(
                ok=False,
                rows_checked=total,
                first_bad_id=bad,
                message=(
                    f"chain has {len(starts)} genesis rows; an intact "
                    "chain has exactly one all-zero prev_event_hash"
                ),
            )

    # Walk each fragment from its start, following successors. Every
    # loaded row must be reached exactly once; a shortfall means a row
    # is unreachable — an orphaned fragment or a cycle.
    hmac_of = {row_id: h for row_id, _p, h in loaded}
    visited: set[int] = set()
    for start_rid, _prev in starts:
        cur: int | None = start_rid
        while cur is not None and cur not in visited:
            visited.add(cur)
            cur = by_prev.get(hmac_of[cur])

    if len(visited) != total:
        orphan = min(set(hmac_of) - visited)
        return ChainVerifyResult(
            ok=False,
            rows_checked=len(visited),
            first_bad_id=orphan,
            message=(
                f"audit_event id={orphan} is unreachable by chain linkage "
                "(orphaned fragment or cycle — chain tampered)"
            ),
        )

    return ChainVerifyResult(
        ok=True,
        rows_checked=total,
        first_bad_id=None,
        message=f"chain intact; {total} row(s) verified",
    )


async def verify_chain(
    session: AsyncSession,
    *,
    start_id: int | None = None,
    end_id: int | None = None,
) -> ChainVerifyResult:
    """Verify the ``audit_event`` HMAC chain over an optional id range.

    Two independent checks:

    * **Per-row HMAC** — recompute every row's HMAC server-side with
      :data:`_ROW_HMAC_SQL` (the trigger's exact canonical payload) and
      compare it to the stored ``hmac``.
    * **Chain linkage** — confirm the rows form a valid linked list by
      following ``prev_event_hash`` pointers. The walk follows *chain*
      links, never ``id`` order: ``id`` is assigned from a sequence
      before the chain advisory lock, so concurrent inserts can commit
      with ``id`` order reversed relative to chain order. That is
      harmless — the chain is still linear — and must not be reported
      as tampering. A genuine fork, orphan, cycle or duplicate *is*
      reported.

    For a full-table verify (no bounds) the rows must form exactly one
    linked list rooted at the single genesis row (all-zero
    ``prev_event_hash``). For a bounded range, rows whose predecessor
    falls outside the range are accepted as fragment starts.

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
            ok=True,
            rows_checked=0,
            first_bad_id=None,
            message="empty range; nothing to verify",
        )

    loaded: list[tuple[int, bytes, bytes]] = [
        (int(r[0]), bytes(r[1]), bytes(r[2])) for r in rows
    ]

    # Per-row HMAC: recompute each row with the trigger's canonical
    # payload. Independent of chain order — done first, in id order.
    # ``verified`` is the count of rows that already matched.
    for verified, (row_id, _prev, stored_hmac) in enumerate(loaded):
        recomputed = bytes(
            (await session.execute(_ROW_HMAC_SQL, {"id": row_id})).scalar_one()
        )
        if recomputed != stored_hmac:
            return ChainVerifyResult(
                ok=False,
                rows_checked=verified,
                first_bad_id=row_id,
                message=f"HMAC mismatch at audit_event id={row_id} (row tampered or key wrong)",
            )

    # Structural linkage check (pure; follows prev_event_hash links).
    return _verify_chain_linkage(
        loaded, full_range=start_id is None and end_id is None
    )
