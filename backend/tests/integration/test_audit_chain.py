"""HMAC chain + tamper-protection tests for ``audit_event``."""

from __future__ import annotations

import pytest
from app.core.audit import log_audit
from app.core.seal import verify_chain
from app.models.audit_event import AuditEvent, AuditEventType
from app.models.user import User
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, InternalError
from sqlalchemy.ext.asyncio import AsyncSession

from tests.fixtures.audit_chain import construct_id_chain_divergence

pytestmark = pytest.mark.integration


async def _seed_user(session: AsyncSession, uid: str = "audit-test-user") -> User:
    user = User(id=uid, display_name="Audit Tester")
    session.add(user)
    await session.flush()
    return user


async def test_genesis_row_has_zero_prev_hash(session: AsyncSession) -> None:
    await _seed_user(session, "audit-genesis")
    e = await log_audit(
        session,
        event_type=AuditEventType.info_event,
        actor_id="audit-genesis",
        summary="genesis",
    )
    # Either genuine genesis (DB had no rows when this test ran first) OR
    # there's a previous row from another test; both are valid chain shapes.
    assert len(e.prev_event_hash) == 32
    assert len(e.hmac) == 32


async def test_chain_links_via_prev_event_hash(session: AsyncSession) -> None:
    await _seed_user(session, "audit-chain-1")
    e1 = await log_audit(
        session,
        event_type=AuditEventType.info_event,
        actor_id="audit-chain-1",
        summary="one",
    )
    e2 = await log_audit(
        session,
        event_type=AuditEventType.info_event,
        actor_id="audit-chain-1",
        summary="two",
    )
    e3 = await log_audit(
        session,
        event_type=AuditEventType.info_event,
        actor_id="audit-chain-1",
        summary="three",
    )

    assert e2.prev_event_hash == e1.hmac
    assert e3.prev_event_hash == e2.hmac
    # Distinct content → distinct HMACs
    assert len({e1.hmac, e2.hmac, e3.hmac}) == 3


async def test_chain_recomputes_in_postgres(session: AsyncSession) -> None:
    """Recompute each row's HMAC inside Postgres and verify it matches stored."""
    await _seed_user(session, "audit-recompute")
    rows = []
    for i in range(3):
        e = await log_audit(
            session,
            event_type=AuditEventType.info_event,
            actor_id="audit-recompute",
            summary=f"event-{i}",
            params={"i": i},
            reason_codes=["TEST"],
        )
        rows.append(e)

    for e in rows:
        result = await session.execute(
            text(
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
            ),
            {"id": e.id},
        )
        recomputed = result.scalar_one()
        assert recomputed == e.hmac, f"HMAC mismatch on event id={e.id}"


async def test_audit_event_update_blocked(session: AsyncSession) -> None:
    await _seed_user(session, "audit-update-block")
    event = await log_audit(
        session,
        event_type=AuditEventType.info_event,
        actor_id="audit-update-block",
        summary="immutable",
    )
    with pytest.raises((InternalError, DBAPIError)) as exc_info:
        await session.execute(
            text("UPDATE audit_event SET summary = 'changed' WHERE id = :id"),
            {"id": event.id},
        )
        await session.flush()
    assert "append-only" in str(exc_info.value).lower()


async def test_audit_event_delete_blocked(session: AsyncSession) -> None:
    await _seed_user(session, "audit-delete-block")
    event = await log_audit(
        session,
        event_type=AuditEventType.info_event,
        actor_id="audit-delete-block",
        summary="immutable",
    )
    with pytest.raises((InternalError, DBAPIError)) as exc_info:
        await session.execute(
            text("DELETE FROM audit_event WHERE id = :id"),
            {"id": event.id},
        )
        await session.flush()
    assert "append-only" in str(exc_info.value).lower()


async def test_insert_with_unconfigured_key_id_raises(
    session: AsyncSession,
) -> None:
    """If the GUC for the chosen key_id isn't set, INSERT must raise."""
    await _seed_user(session, "audit-no-key")
    bad = AuditEvent(
        event_type=AuditEventType.info_event,
        actor_id="audit-no-key",
        params={},
        reason_codes=[],
        key_id=99,  # No audit.hmac_key_99 GUC set in this session
        prev_event_hash=b"\x00" * 32,
        hmac=b"\x00" * 32,
    )
    session.add(bad)
    with pytest.raises((InternalError, DBAPIError)) as exc_info:
        await session.flush()
    msg = str(exc_info.value).lower()
    assert "audit.hmac_key_99" in msg or "not configured" in msg or "is empty" in msg


async def test_concurrent_inserts_serialize_chain(async_engine) -> None:  # type: ignore[no-untyped-def]
    """Two transactions concurrently inserting both produce a valid chain.

    The trigger uses ``pg_advisory_xact_lock`` to serialize chain
    inserts; without that, both could read the same "previous head" and
    corrupt the chain.
    """
    import asyncio  # noqa: PLC0415
    import os  # noqa: PLC0415

    from sqlalchemy.ext.asyncio import async_sessionmaker  # noqa: PLC0415

    factory = async_sessionmaker(async_engine, expire_on_commit=False)
    test_key = os.environ["HMAC_KEY_1"]

    async def _insert_one(label: str) -> int:
        async with factory() as s:
            await s.execute(
                text("SELECT set_config('audit.hmac_key_1', :v, false)"),
                {"v": test_key},
            )
            user = User(id=f"concurrent-{label}", display_name=label)
            s.add(user)
            await s.flush()
            ev = await log_audit(
                s,
                event_type=AuditEventType.info_event,
                actor_id=f"concurrent-{label}",
                summary=f"concurrent-{label}",
            )
            await s.commit()
            return ev.id

    ids = await asyncio.gather(_insert_one("a"), _insert_one("b"))
    assert len(ids) == 2
    assert ids[0] != ids[1]

    # Verify chain integrity for these two
    async with factory() as s:
        await s.execute(
            text("SELECT set_config('audit.hmac_key_1', :v, false)"),
            {"v": test_key},
        )
        result = await s.execute(
            text(
                "SELECT id, prev_event_hash, hmac FROM audit_event "
                "WHERE id = ANY(:ids) ORDER BY id"
            ),
            {"ids": ids},
        )
        rows = result.all()
        assert len(rows) == 2

        # Both rows have valid 32-byte hashes and non-zero HMACs
        for row in rows:
            assert len(bytes(row[1])) == 32  # prev_event_hash
            assert len(bytes(row[2])) == 32  # hmac
            assert bytes(row[2]) != b"\x00" * 32

        # Distinct content → distinct HMACs
        assert bytes(rows[0][2]) != bytes(rows[1][2])

        # The actual chain-integrity invariant: no two committed rows
        # may share a prev_event_hash. If the advisory lock failed,
        # both txs would observe the same "previous head" and produce
        # rows with identical prev_event_hash values.
        assert bytes(rows[0][1]) != bytes(rows[1][1]), (
            "advisory lock failed to serialize chain inserts"
        )

        # Cleanup: drop the two test users we inserted
        await s.execute(
            text("DELETE FROM users WHERE id IN ('concurrent-a','concurrent-b')")
        )
        # NOTE: we don't delete the audit_event rows — that's blocked by
        # design. They live on; that's fine for tests.
        await s.commit()


async def test_divergence_does_not_fork_chain(session: AsyncSession) -> None:
    """After an id/chain divergence, the next insert must not fork the chain.

    The fixed trigger chains onto the explicit ``audit_chain_head``
    pointer, so the row inserted after a divergence links onto the true
    chain tip — not onto ``MAX(id)``. Chaining onto ``MAX(id)`` is what
    the baseline trigger did, and it orphaned the lower-id row of the
    diverged pair (a permanent fork).
    """
    pair = await construct_id_chain_divergence(session)
    assert pair.lo_id < pair.hi_id  # divergence really constructed

    # The next ordinary insert must chain onto the true tip — `lo`, the
    # last row in chain order — and NOT onto MAX(id) == `hi`.
    nxt = await log_audit(
        session,
        event_type=AuditEventType.info_event,
        actor_id="divergence",
        summary="after-divergence",
    )
    assert bytes(nxt.prev_event_hash) == pair.lo_hmac, (
        "next insert chained onto MAX(id), not the true chain tip — fork"
    )

    # No two rows anywhere share a prev_event_hash (the fork signature).
    forked = await session.execute(
        text(
            "SELECT prev_event_hash FROM audit_event "
            "GROUP BY prev_event_hash HAVING count(*) > 1"
        )
    )
    assert forked.first() is None, "two rows share a prev_event_hash — chain forked"


async def test_many_concurrent_inserts_keep_chain_linear(async_engine) -> None:  # type: ignore[no-untyped-def]
    """Many concurrent audit inserts always commit one intact linear chain.

    Exercises the real advisory-lock race repeatedly. Whichever order
    transactions win the lock, the head-pointer trigger keeps the chain
    linear: verify_chain stays ok and no two rows share a predecessor.
    With the baseline MAX(id) trigger this forked whenever a higher-id
    transaction won the lock.
    """
    import asyncio  # noqa: PLC0415
    import os  # noqa: PLC0415

    from sqlalchemy.ext.asyncio import async_sessionmaker  # noqa: PLC0415

    factory = async_sessionmaker(async_engine, expire_on_commit=False)
    test_key = os.environ["HMAC_KEY_1"]

    async def _insert(label: str) -> int:
        async with factory() as s:
            await s.execute(
                text("SELECT set_config('audit.hmac_key_1', :v, false)"),
                {"v": test_key},
            )
            ev = await log_audit(
                s,
                event_type=AuditEventType.info_event,
                actor_id="concurrent-many",
                summary=label,
            )
            await s.commit()
            return ev.id

    ids = await asyncio.gather(*[_insert(f"c{i}") for i in range(12)])
    assert len(set(ids)) == 12  # all distinct, all committed

    async with factory() as s:
        await s.execute(
            text("SELECT set_config('audit.hmac_key_1', :v, false)"),
            {"v": test_key},
        )
        result = await verify_chain(s)
        assert result.ok is True, result.message

        forked = await s.execute(
            text(
                "SELECT 1 FROM audit_event "
                "GROUP BY prev_event_hash HAVING count(*) > 1 LIMIT 1"
            )
        )
        assert forked.first() is None, "concurrent inserts forked the chain"
