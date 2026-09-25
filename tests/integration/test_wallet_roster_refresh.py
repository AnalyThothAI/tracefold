"""Complete membership, durable retries and continuous monitoring on PostgreSQL."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

import pytest

from tests.integration.test_news_chain_tape import _Db
from tests.postgres_test_utils import connect_postgres_test
from tracefold.app.repository_session import repositories_for_connection
from tracefold.integrations.robinhoodtrenches import RosterProviderError
from tracefold.news.chain_tape.contracts import RosterMember
from tracefold.news.chain_tape.roster_refresh import RosterRefreshLoop


@pytest.fixture()
def conn(postgres_clone_dsn: str):
    connection = connect_postgres_test(read_only=False)
    yield connection
    connection.close()


pytestmark = pytest.mark.integration
NOW = 1_900_000_000_000


class Site:
    last_response_bytes = 0

    def __init__(self, count: int = 147) -> None:
        self.rows = tuple(RosterMember(f"0x{i:040x}", f"wallet{i}") for i in range(1, count + 1))
        self.calls = 0
        self.failure: Exception | None = None

    async def traders(self, *, window: str):
        self.calls += 1
        if self.failure:
            raise self.failure
        return self.rows


def loop(conn: Any, site: Site, clock: list[int]) -> RosterRefreshLoop:
    return RosterRefreshLoop(db=_Db(conn), provider=site, clock=lambda: clock[0])


@pytest.mark.parametrize("count", [147, 201, 256])
def test_complete_list_published_and_collected_without_rank_limit(conn: Any, count: int) -> None:
    site, clock = Site(count), [NOW]
    result = asyncio.run(loop(conn, site, clock).advance())
    news = repositories_for_connection(conn).news
    assert result["published"] and site.calls == 1
    assert news.chain_tape_current_roster().wallets == tuple(m.wallet for m in site.rows)
    assert news.chain_tape_collection_wallets(through_at_ms=NOW) == tuple(m.wallet for m in site.rows)
    assert news.chain_tape_state()["roster_last_success_at_ms"] == NOW


def test_alias_change_is_not_membership_change_and_preserves_monitoring(conn: Any) -> None:
    site, clock = Site(5), [NOW]
    task = loop(conn, site, clock)
    asyncio.run(task.advance())
    conn.execute("UPDATE news_market_wallet_roster SET monitoring_from_ms = %s", (NOW,))
    conn.commit()
    site.rows = tuple(replace(m, handle="new alias") for m in site.rows)
    clock[0] += 3_600_000
    asyncio.run(task.advance())
    rows = repositories_for_connection(conn).news.chain_tape_roster_rows()
    assert {r["roster_version"] for r in rows} == {1}
    assert {r["monitoring_from_ms"] for r in rows} == {NOW}
    assert {r["handle"] for r in rows} == {"new alias"}


def test_failure_preserves_list_success_time_and_survives_restart(conn: Any) -> None:
    site, clock = Site(5), [NOW]
    asyncio.run(loop(conn, site, clock).advance())
    clock[0] += 3_600_000
    site.failure = RosterProviderError("roster_rate_limited", retry_after_ms=900_000)
    asyncio.run(loop(conn, site, clock).advance())
    news = repositories_for_connection(conn).news
    state = news.chain_tape_state()
    assert state["roster_last_success_at_ms"] == NOW
    assert state["roster_last_attempt_at_ms"] == clock[0]
    assert state["roster_next_attempt_at_ms"] == clock[0] + 900_000
    assert news.chain_tape_current_roster().roster_version == 1
    assert not asyncio.run(loop(conn, site, clock).advance())["due"]
    assert site.calls == 2
    clock[0] += 900_000
    site.failure = None
    assert asyncio.run(loop(conn, site, clock).advance())["published"]
    assert news.chain_tape_state()["roster_consecutive_failures"] == 0


def test_empty_source_does_not_erase_previous_membership(conn: Any) -> None:
    site, clock = Site(5), [NOW]
    asyncio.run(loop(conn, site, clock).advance())
    site.rows = ()
    clock[0] += 3_600_000
    assert not asyncio.run(loop(conn, site, clock).advance())["published"]
    assert len(repositories_for_connection(conn).news.chain_tape_current_roster().members) == 5


def test_attempt_and_completion_are_different_facts(conn: Any) -> None:
    clock = [NOW]

    class SlowSite(Site):
        async def traders(self, *, window: str):
            state = repositories_for_connection(conn).news.chain_tape_state()
            assert state["roster_last_attempt_at_ms"] == NOW
            assert state["roster_last_success_at_ms"] is None
            clock[0] += 1234
            return await super().traders(window=window)

    asyncio.run(loop(conn, SlowSite(5), clock).advance())
    state = repositories_for_connection(conn).news.chain_tape_state()
    assert state["roster_last_attempt_at_ms"] == NOW
    assert state["roster_last_success_at_ms"] == NOW + 1234


def test_membership_change_and_rejoin_only_inherit_continuous_collection(conn: Any) -> None:
    news = repositories_for_connection(conn).news
    a, b = Site(2).rows
    with conn.transaction():
        news.chain_tape_store_roster((a, b), now_ms=NOW)
        news.chain_tape_begin_roster_refresh(now_ms=NOW, next_attempt_at_ms=NOW)
        news.chain_tape_record_coverage(
            from_ms=NOW,
            through_ms=NOW,
            through_block=1,
            through_log=100,
            gap_at_ms=None,
            wallets=(a.wallet, b.wallet),
            roster_version=1,
        )
        news.chain_tape_store_roster((a,), now_ms=NOW + 1000)
        assert b.wallet in news.chain_tape_collection_wallets(through_at_ms=NOW + 2000)
        news.chain_tape_store_roster((a, b), now_ms=NOW + 2000)
    assert {r["monitoring_from_ms"] for r in news.chain_tape_members(3)} == {NOW}
    with conn.transaction():
        news.chain_tape_store_roster((a,), now_ms=NOW + 3000)
        conn.execute("UPDATE news_market_wallet_tape_state SET scanned_at_ms=%s", (NOW + 1_900_000,))
        news.chain_tape_store_roster((a, b), now_ms=NOW + 2_000_000)
    rows = {r["wallet"]: r for r in news.chain_tape_members(5)}
    assert rows[a.wallet]["monitoring_from_ms"] == NOW
    assert rows[b.wallet]["monitoring_from_ms"] is None


def test_old_collection_turn_cannot_mark_concurrently_added_version_monitored(conn: Any) -> None:
    news = repositories_for_connection(conn).news
    a, b = Site(2).rows
    with conn.transaction():
        news.chain_tape_store_roster((a,), now_ms=NOW)
        news.chain_tape_begin_roster_refresh(now_ms=NOW, next_attempt_at_ms=NOW)
        news.chain_tape_store_roster((a, b), now_ms=NOW + 1)
        news.chain_tape_record_coverage(
            from_ms=NOW,
            through_ms=NOW + 100,
            through_block=10,
            through_log=100,
            gap_at_ms=None,
            wallets=(a.wallet,),
            roster_version=1,
        )
    assert all(r["monitoring_from_ms"] is None for r in news.chain_tape_members(2))
