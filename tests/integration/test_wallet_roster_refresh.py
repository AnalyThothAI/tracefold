"""The `news-wallet-roster` task against real PostgreSQL (#649 §5.1, §5.2).

What is proved here is what only a database can answer: that a refresh which could not talk to the
site publishes nothing and leaves the previous version -- and its own `taken_at_ms` -- exactly as it
was; that a handle the site does not have is an ordinary unknown and not a failure; that a version
appears only when the list really changed; and that a statistics-only change inherits `known_at_ms`
and `monitoring_from_ms` rather than restarting an address's warm-up.

The collector's own regressions are next door in `test_news_chain_tape.py`, which since this task
exists never calls the roster site at all.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from tests.postgres_test_utils import connect_postgres_test
from tracefold.app.repository_session import repositories_for_connection
from tracefold.news.bus import TransientError
from tracefold.news.chain_tape.contracts import RosterMember
from tracefold.news.chain_tape.roster_refresh import RosterRefreshLoop

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "chain_tape"
WALLET_A = "0x69326e48f68500fb6cf3b3a7da640737b9cc347b"
WALLET_B = "0x80f3b0b712a82172a67e454e313ba6e2b0e7ae64"
HOUR_MS = 3_600_000


@pytest.fixture()
def conn(postgres_clone_dsn: str):
    connection = connect_postgres_test(read_only=False)
    yield connection
    connection.close()


class _Db:
    def __init__(self, connection: Any) -> None:
        self.connection = connection
        self.fail_on: dict[str, Exception] = {}

    async def read(self, name: str, fn: Callable[[Any], Any], *, timeout_seconds: float = 3.0) -> Any:
        if name in self.fail_on:
            raise self.fail_on[name]
        return fn(repositories_for_connection(self.connection))

    async def tx(self, name: str, fn: Callable[[Any], Any], *, timeout_seconds: float = 3.0) -> Any:
        if name in self.fail_on:
            raise self.fail_on[name]
        repos = repositories_for_connection(self.connection)
        with repos.transaction():
            return fn(repos)


@dataclass(frozen=True, slots=True)
class _Candidate:
    address: str
    handle: str
    followers: int = 1
    realized_pnl: float = 1.0
    closed_trades: int = 20
    win_rate: float = 0.5
    open_cost: float = 5.0


@dataclass(frozen=True, slots=True)
class _Stats:
    handle: str
    profit_factor: float | None


class _Site:
    """The roster site, answering with whatever the test wants -- including refusing to answer."""

    def __init__(
        self,
        rows: Sequence[Any],
        *,
        factors: dict[str, float | None] | None = None,
        missing: Sequence[str] = (),
        fail_on_handles: dict[str, Exception] | None = None,
    ) -> None:
        self.rows = list(rows)
        self.factors = dict(factors or {})
        self.missing = set(missing)
        self.fail_on_handles = dict(fail_on_handles or {})
        self.fail_list: Exception | None = None
        self.last_response_bytes = 0
        self.windows: list[str] = []
        self.handles: list[str] = []

    async def traders(self, *, window: str = "7d") -> tuple[Any, ...]:
        self.windows.append(window)
        if self.fail_list is not None:
            raise self.fail_list
        return tuple(self.rows)

    async def trader(self, handle: str, *, window: str = "7d") -> Any | None:
        self.windows.append(window)
        self.handles.append(handle)
        if handle in self.fail_on_handles:
            raise self.fail_on_handles[handle]
        if handle in self.missing:
            return None
        return _Stats(handle, self.factors.get(handle))


def _member(wallet: str, *, quality: int | None = 1, factor: float = 1.4) -> RosterMember:
    return RosterMember(
        wallet=wallet,
        handle=f"handle-{wallet[-4:]}",
        followers=1_000,
        realized_pnl=1.5,
        closed_trades=20,
        win_rate=0.5,
        profit_factor=factor,
        open_cost=2.0,
        rank_quality=quality,
        rank_whale=None,
    )


def _seed(conn: Any, members: Sequence[RosterMember], *, now_ms: int) -> int:
    repos = repositories_for_connection(conn)
    with repos.transaction():
        snapshot = repos.news.chain_tape_store_roster(list(members), now_ms=now_ms)
    return snapshot.roster_version


def _loop(conn: Any, site: _Site, *, now_ms: int, **kwargs: Any) -> RosterRefreshLoop:
    return RosterRefreshLoop(
        db=kwargs.pop("db", None) or _Db(conn),
        provider=site,
        clock=lambda: now_ms,
        **kwargs,
    )


def _state(conn: Any) -> dict[str, Any]:
    return dict(repositories_for_connection(conn).news.chain_tape_state() or {})


def _current(conn: Any) -> Any:
    return repositories_for_connection(conn).news.chain_tape_current_roster()


# --------------------------------------------------------------------- publishing
def test_a_complete_refresh_publishes_the_list_and_stamps_its_success(conn: Any) -> None:
    site = _Site(
        [_Candidate(WALLET_A, "alice"), _Candidate(WALLET_B, "bob")],
        factors={"alice": 3.2, "bob": 0.4},
    )
    loop = _loop(conn, site, now_ms=5 * HOUR_MS, window="30d")

    result = asyncio.run(loop.advance())

    assert result["published"] is True
    assert result["candidates"] == 2
    assert result["profit_factor_known"] == 2
    # Both endpoints are asked for the same statistics window; the deployed code used to request the
    # list at 7d and the per-trader document with no window at all (#649 §2.1).
    assert set(site.windows) == {"30d"}
    current = _current(conn)
    assert current.roster_version == 1
    by_handle = {member.handle: member for member in current.members}
    assert by_handle["alice"].rank_quality == 1
    assert by_handle["bob"].rank_quality is None
    state = _state(conn)
    assert state["roster_last_success_at_ms"] == 5 * HOUR_MS
    assert state["roster_last_error"] is None


def test_a_refresh_is_not_due_until_the_period_since_the_last_success_has_passed(conn: Any) -> None:
    _seed(conn, [_member(WALLET_A)], now_ms=HOUR_MS)
    site = _Site([_Candidate(WALLET_A, "alice")], factors={"alice": 3.2})

    quiet = asyncio.run(_loop(conn, site, now_ms=HOUR_MS + 60_000).advance())

    assert quiet["due"] is False
    assert site.windows == []


# --------------------------------------------------------------------- failure is not a publication
def test_a_rate_limited_profit_factor_publishes_nothing_and_keeps_the_previous_version(conn: Any) -> None:
    """§11: PF partially 429 -> no new version, previous list and its real `taken_at` preserved.

    This is the production defect in one test. `bob`'s document answers 429; under the old in-loop
    refresh that became "profit factor unknown", `bob` fell out of the quality pool, and the thinner
    list was published as a new version anyway.
    """

    published_at = 1_000 * HOUR_MS
    version = _seed(conn, [_member(WALLET_A), _member(WALLET_B, quality=2)], now_ms=published_at)
    site = _Site(
        [_Candidate(WALLET_A, "alice"), _Candidate(WALLET_B, "bob")],
        factors={"alice": 3.2},
        fail_on_handles={"bob": _rate_limited()},
    )
    loop = _loop(conn, site, now_ms=published_at + 2 * HOUR_MS)

    result = asyncio.run(loop.advance())

    assert result["published"] is False
    current = _current(conn)
    assert current.roster_version == version
    assert current.taken_at_ms == published_at
    assert [member.wallet for member in current.members] == sorted([WALLET_A, WALLET_B])
    state = _state(conn)
    assert state["roster_last_attempt_at_ms"] == published_at + 2 * HOUR_MS
    # The success stamp may never move on a failure: it is how an operator decides whether the list
    # they are looking at is still the truth.
    assert state["roster_last_success_at_ms"] is None
    assert state["roster_last_error"] == "robinhoodtrenches:roster_rate_limited"


def test_a_list_request_that_did_not_answer_stops_the_refresh_before_any_lookup(conn: Any) -> None:
    published_at = 1_000 * HOUR_MS
    version = _seed(conn, [_member(WALLET_A)], now_ms=published_at)
    site = _Site([_Candidate(WALLET_A, "alice")], factors={"alice": 3.2})
    site.fail_list = _timed_out()

    result = asyncio.run(_loop(conn, site, now_ms=published_at + 2 * HOUR_MS).advance())

    assert (result["published"], result["candidates"]) == (False, 0)
    assert site.handles == []
    assert _current(conn).roster_version == version
    assert _state(conn)["roster_last_error"] == "robinhoodtrenches:roster_timeout"


def test_a_handle_the_site_does_not_have_is_unknown_and_not_a_refresh_failure(conn: Any) -> None:
    """§11: an explicit 404 is different from a call that did not answer.

    `bob` is genuinely gone from the site, so his factor is unknown, he cannot pass the quality rule,
    and the list is published without him. Freezing the roster because one address disappeared would
    stop every ordinary membership change.
    """

    site = _Site(
        [_Candidate(WALLET_A, "alice"), _Candidate(WALLET_B, "bob")],
        factors={"alice": 3.2},
        missing=["bob"],
    )

    result = asyncio.run(_loop(conn, site, now_ms=5 * HOUR_MS).advance())

    assert result["published"] is True
    assert (result["profit_factor_known"], result["profit_factor_unknown"]) == (1, 1)
    quality = {member.handle for member in _current(conn).members if member.rank_quality is not None}
    assert quality == {"alice"}
    assert _state(conn)["roster_last_error"] is None


def test_a_refused_publish_keeps_the_previous_version_and_records_the_failure(conn: Any) -> None:
    published_at = 1_000 * HOUR_MS
    version = _seed(conn, [_member(WALLET_A)], now_ms=published_at)
    db = _Db(conn)
    db.fail_on = {"news_chain_tape_roster": TransientError("db_overrun")}
    site = _Site([_Candidate(WALLET_A, "alice"), _Candidate(WALLET_B, "bob")], factors={"alice": 3.2, "bob": 2.0})
    loop = _loop(conn, site, now_ms=published_at + 2 * HOUR_MS, db=db)

    result = asyncio.run(loop.advance())

    assert result["published"] is False
    assert loop.last_error == "db:TransientError"
    current = _current(conn)
    assert current.roster_version == version
    assert current.taken_at_ms == published_at


def test_a_selection_that_chose_nobody_is_not_published_as_an_empty_roster(conn: Any) -> None:
    published_at = 1_000 * HOUR_MS
    version = _seed(conn, [_member(WALLET_A)], now_ms=published_at)
    loop = _loop(conn, _Site([]), now_ms=published_at + 2 * HOUR_MS)

    result = asyncio.run(loop.advance())

    assert result["published"] is False
    assert _current(conn).roster_version == version
    assert _state(conn)["roster_last_error"] == "roster_selected_nobody"


# --------------------------------------------------------------------- §5.2 eligibility vs monitoring
def test_a_statistics_only_refresh_inherits_monitoring_and_does_not_restart_the_warm_up(conn: Any) -> None:
    """§5.2: refreshing a number is not meeting an address for the first time.

    Production v203 still carried `game_for_one`'s 09-12 `monitoring_from`, which is the behaviour
    this pins: a new version caused by a profit factor moving keeps both stamps, so the address does
    not lose the thirty minutes of monitoring support the slow window needs.
    """

    first_seen = 1_000 * HOUR_MS
    _seed(conn, [_member(WALLET_A, factor=1.4)], now_ms=first_seen)
    repos = repositories_for_connection(conn)
    with repos.transaction():
        # The collector's own write: this address has been covered since it was first published.
        repos.news.chain_tape_record_coverage(
            from_ms=first_seen,
            through_ms=first_seen,
            through_block=10,
            through_log=1,
            gap_at_ms=None,
            wallets=[WALLET_A],
        )
    conn.commit()
    before = repos.news.chain_tape_members(1)[0]
    assert before["monitoring_from_ms"] == first_seen

    site = _Site([_Candidate(WALLET_A, "handle-347b")], factors={"handle-347b": 9.9})
    published = asyncio.run(_loop(conn, site, now_ms=first_seen + 2 * HOUR_MS).advance())

    assert published["published"] is True
    after = repos.news.chain_tape_members(2)[0]
    assert after["roster_version"] == 2
    assert float(after["profit_factor"]) == 9.9
    assert after["monitoring_from_ms"] == first_seen
    # `known_at_ms` is the *version's* stamp, not the address's first sighting: it is what
    # `chain_tape_collection_wallets` orders versions by, so that a removed address keeps being
    # collected until its last supported thirty-minute window has been scanned. The stamp that
    # carries an address's warm-up is `monitoring_from_ms`, and that is the one inherited above.
    assert after["known_at_ms"] == first_seen + 2 * HOUR_MS


def test_a_refresh_concurrent_with_collection_keeps_both_writes(conn: Any) -> None:
    """A refresh and a collection turn touch the same roster rows; neither may erase the other.

    The collector writes `monitoring_from_ms` for the wallets it covered; the refresh writes a new
    version. Interleaved, the new version inherits the monitoring the collector had just recorded.
    """

    first_seen = 1_000 * HOUR_MS
    _seed(conn, [_member(WALLET_A)], now_ms=first_seen)
    site = _Site(
        [_Candidate(WALLET_A, "handle-347b"), _Candidate(WALLET_B, "bob")], factors={"handle-347b": 2.0, "bob": 3.0}
    )
    repos = repositories_for_connection(conn)
    with repos.transaction():
        repos.news.chain_tape_record_coverage(
            from_ms=first_seen,
            through_ms=first_seen,
            through_block=10,
            through_log=1,
            gap_at_ms=None,
            wallets=[WALLET_A],
        )
    conn.commit()

    asyncio.run(_loop(conn, site, now_ms=first_seen + 2 * HOUR_MS).advance())

    members = {row["wallet"]: row for row in repos.news.chain_tape_members(2)}
    assert members[WALLET_A]["monitoring_from_ms"] == first_seen
    # A genuinely new address has no monitoring support yet, and saying so is the point: the rule
    # refuses it until collection has actually covered a window for it.
    assert members[WALLET_B]["monitoring_from_ms"] is None


def test_a_restart_re_reads_the_published_version_rather_than_re_warming_it(conn: Any) -> None:
    first_seen = 1_000 * HOUR_MS
    _seed(conn, [_member(WALLET_A)], now_ms=first_seen)
    repos = repositories_for_connection(conn)
    with repos.transaction():
        repos.news.chain_tape_record_coverage(
            from_ms=first_seen,
            through_ms=first_seen,
            through_block=10,
            through_log=1,
            gap_at_ms=None,
            wallets=[WALLET_A],
        )
    conn.commit()
    site = _Site([_Candidate(WALLET_A, "handle-347b")], factors={"handle-347b": 1.4})

    # Two separate process lifetimes over the same database, two hours apart.
    asyncio.run(_loop(conn, site, now_ms=first_seen + 2 * HOUR_MS).advance())
    published = _current(conn).roster_version
    asyncio.run(_loop(conn, site, now_ms=first_seen + 4 * HOUR_MS).advance())

    current = _current(conn)
    # The second refresh selected exactly the same members and ranks, so no version was opened and
    # only the fetch time moved. A restart is not a reason to re-warm an address.
    assert current.roster_version == published
    assert current.taken_at_ms == first_seen + 4 * HOUR_MS
    assert repos.news.chain_tape_members(published)[0]["monitoring_from_ms"] == first_seen


# --------------------------------------------------------------------- the recorded site
def test_the_recorded_site_answers_produce_the_expected_two_lists(conn: Any) -> None:
    rows = json.loads((FIXTURES / "traders_window_7d.json").read_text(encoding="utf-8"))
    stats = json.loads((FIXTURES / "trader_stats.json").read_text(encoding="utf-8"))
    site = _Site(
        [
            _Candidate(
                address=str(row["address"]),
                handle=str(row["handle"]),
                followers=int(row["followers"]),
                realized_pnl=float(row["realized_pnl"]),
                closed_trades=int(row["closed_trades"]),
                win_rate=float(row["win_rate"]),
                open_cost=float(row["open_cost"]),
            )
            for row in rows
        ],
        factors={handle: document["stats"].get("profit_factor") for handle, document in stats.items()},
    )

    asyncio.run(_loop(conn, site, now_ms=5 * HOUR_MS).advance())

    current = _current(conn)
    assert current.roster_version == 1
    by_handle = {member.handle: member for member in current.members}
    assert by_handle["frankdegods"].rank_quality == 1
    assert by_handle["FartmanSacks"].rank_whale == 1
    assert by_handle["FartmanSacks"].rank_quality is None


def _rate_limited() -> Exception:
    from tracefold.integrations.robinhoodtrenches import RosterProviderError

    return RosterProviderError("roster_rate_limited", status_code=429)


def _timed_out() -> Exception:
    from tracefold.integrations.robinhoodtrenches import RosterProviderError

    return RosterProviderError("roster_timeout")
