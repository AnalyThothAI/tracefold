from __future__ import annotations

import asyncio
from typing import Any

from tests.factories import make_event
from tests.postgres_test_utils import connect_postgres_test, repository_session_for_connection
from tests.postgres_test_utils import reset_postgres_schema as migrate
from tracefold.app.repositories import repositories_for_connection
from tracefold.market import DexProviderTemporarilyUnavailable, DexTokenCandidate, IngestService, ResolutionRefresh
from tracefold.platform.resource import ResourceAdmissionTimeout

NOW_MS = 1_778_145_100_000
ADDRESS = "0x44b28991b167582f18ba0259e0173176ca125505"


class _InlineDatabase:
    def __init__(self, conn: Any) -> None:
        self.conn = conn

    async def run_business(
        self,
        _operation_name: str,
        function: Any,
        /,
        *args: Any,
        operation_timeout_seconds: float,
        **kwargs: Any,
    ) -> Any:
        del operation_timeout_seconds
        on_submitted = kwargs.pop("on_submitted", None)
        if on_submitted is not None:
            on_submitted()
        return function(*args, **kwargs)

    def worker_session(self, _name: str, _timeout_seconds: float | None = None):
        return repository_session_for_connection(self.conn)


class _InlineFiniteOperations:
    async def run(
        self,
        _operation_name: str,
        function: Any,
        /,
        *args: Any,
        timeout_seconds: float,
        on_submitted: Any = None,
        **kwargs: Any,
    ) -> Any:
        del timeout_seconds
        if on_submitted is not None:
            on_submitted()
        return function(*args, **kwargs)


class _RejectingFiniteOperations:
    async def run(self, *_args: Any, **_kwargs: Any) -> Any:
        raise ResourceAdmissionTimeout("finite_operation_admission_timeout:test")


class _DexMarket:
    def __init__(self, *, candidates: list[DexTokenCandidate]) -> None:
        self.candidates = candidates
        self.search_requests = 0

    def search_tokens(self, *, query: str, chain_ids: tuple[str, ...]) -> list[DexTokenCandidate]:
        del query, chain_ids
        self.search_requests += 1
        return list(self.candidates)


class _UnavailableDexMarket:
    def __init__(self) -> None:
        self.search_requests = 0

    def search_tokens(self, *, query: str, chain_ids: tuple[str, ...]) -> list[DexTokenCandidate]:
        del query, chain_ids
        self.search_requests += 1
        raise DexProviderTemporarilyUnavailable("provider unavailable")


def _candidate(symbol: str) -> DexTokenCandidate:
    return DexTokenCandidate(
        chain_id="eip155:1",
        address=ADDRESS,
        symbol=symbol,
        name=symbol,
        price_usd=1.0,
        market_cap_usd=1_000_000.0,
        liquidity_usd=100_000.0,
        holders=1_000,
        community_recognized=True,
        raw={"tokenSymbol": symbol},
    )


def _ingest_service(conn: Any) -> IngestService:
    repos = repositories_for_connection(conn)
    return IngestService(
        evidence=repos.evidence,
        entities=repos.entities,
        registry=repos.registry,
        identity_evidence=repos.identity_evidence,
        token_intent_lookup=repos.token_intent_lookup,
        token_evidence=repos.token_evidence,
        token_intents=repos.token_intents,
        intent_resolutions=repos.intent_resolutions,
        discovery=repos.discovery,
        market_ticks=repos.market_ticks,
        market_tick_current=repos.market_tick_current,
        enriched_events=repos.enriched_events,
        event_anchor_jobs=repos.event_anchor_jobs,
        radar_source_edges=repos.radar_source_edges,
        persisted_live=repos.persisted_live,
        transaction=repos.transaction,
        event_anchor_active_window_ms=300_000,
    )


def _worker(
    conn: Any,
    market: Any,
    *,
    reprocess_limit: int | None = None,
    finite: Any = None,
) -> ResolutionRefresh:
    kwargs = {} if reprocess_limit is None else {"reprocess_limit": reprocess_limit}
    return ResolutionRefresh(
        db=_InlineDatabase(conn),
        dex_discovery_market=market,
        finite_operations=finite or _InlineFiniteOperations(),
        runtime_id="integration-test",
        chain_ids=("eip155:1",),
        **kwargs,
    )


def test_resolution_refresh_publishes_provider_fact_and_resolution_atomically(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        ingested = _ingest_service(conn).ingest_event(
            make_event("event-upeg", text="$UPEG is moving", received_at_ms=NOW_MS)
        )
        market = _DexMarket(candidates=[_candidate("UPEG")])

        progressed = asyncio.run(_worker(conn, market).turn(now_ms=NOW_MS + 60_000))

        resolution = repositories_for_connection(conn).intent_resolutions.active_resolution_for_intent(
            ingested.token_intents[0]["intent_id"]
        )
        discovery = conn.execute(
            """
            SELECT status, candidate_count
              FROM token_discovery_results
             WHERE provider = 'okx_dex_search' AND lookup_key = 'symbol:UPEG'
            """
        ).fetchone()
        queue_count = conn.execute("SELECT COUNT(*) AS count FROM token_discovery_dirty_lookup_keys").fetchone()[
            "count"
        ]
    finally:
        conn.close()

    assert progressed is True
    assert market.search_requests == 1
    assert resolution["resolution_status"] == "UNIQUE_BY_CONTEXT"
    assert resolution["target_id"] == f"asset:eip155:1:erc20:{ADDRESS}"
    assert discovery == {"status": "found", "candidate_count": 1}
    assert queue_count == 0


def test_resolution_refresh_releases_exact_claim_before_provider_submission(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        _ingest_service(conn).ingest_event(make_event("event-wait", text="$WAIT", received_at_ms=NOW_MS))
        market = _DexMarket(candidates=[])
        before = conn.execute(
            "SELECT due_at_ms FROM token_discovery_dirty_lookup_keys WHERE lookup_key = 'symbol:WAIT'"
        ).fetchone()

        disposition = asyncio.run(_worker(conn, market, finite=_RejectingFiniteOperations()).turn(now_ms=NOW_MS + 1))
        after = conn.execute(
            """
            SELECT due_at_ms, attempt_count, lease_owner, leased_until_ms
              FROM token_discovery_dirty_lookup_keys
             WHERE lookup_key = 'symbol:WAIT'
            """
        ).fetchone()
    finally:
        conn.close()

    assert disposition is None
    assert market.search_requests == 0
    assert after == {
        "due_at_ms": before["due_at_ms"],
        "attempt_count": 0,
        "lease_owner": None,
        "leased_until_ms": None,
    }


def test_resolution_refresh_provider_outage_does_not_spend_target_attempt(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        _ingest_service(conn).ingest_event(make_event("event-outage", text="$OUTAGE", received_at_ms=NOW_MS))
        market = _UnavailableDexMarket()

        progressed = asyncio.run(_worker(conn, market).turn(now_ms=NOW_MS + 60_000))
        row = conn.execute(
            """
            SELECT attempt_count, lease_owner, due_at_ms
              FROM token_discovery_dirty_lookup_keys
             WHERE lookup_key = 'symbol:OUTAGE'
            """
        ).fetchone()
    finally:
        conn.close()

    assert progressed is True
    assert market.search_requests == 1
    assert row == {
        "attempt_count": 0,
        "lease_owner": None,
        "due_at_ms": NOW_MS + 90_000,
    }


def test_resolution_refresh_durably_continues_beyond_500_in_bounded_pages_without_refetch(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        ingest = _ingest_service(conn)
        for index in range(501):
            ingest.ingest_event(
                make_event(
                    f"event-bulk-{index:04d}",
                    text="$BULK",
                    received_at_ms=NOW_MS + index,
                )
            )
        market = _DexMarket(candidates=[_candidate("BULK")])
        worker = _worker(conn, market)

        first = asyncio.run(worker.turn(now_ms=NOW_MS + 60_000))
        continuation = conn.execute(
            """
            SELECT reprocess_lookup_keys, reprocess_after_intent_id,
                   reprocess_resolved, attempt_count, lease_owner
              FROM token_discovery_dirty_lookup_keys
             WHERE lookup_key = 'symbol:BULK'
            """
        ).fetchone()
        first_resolved = conn.execute(
            """
            SELECT COUNT(*) AS count
              FROM token_intent_resolutions
             WHERE is_current AND target_id = %s
            """,
            (f"asset:eip155:1:erc20:{ADDRESS}",),
        ).fetchone()["count"]

        turns = 1
        while conn.execute(
            "SELECT EXISTS (SELECT 1 FROM token_discovery_dirty_lookup_keys WHERE lookup_key = 'symbol:BULK') AS open"
        ).fetchone()["open"]:
            turns += 1
            assert turns <= 100
            assert asyncio.run(worker.turn(now_ms=NOW_MS + 60_000 + turns)) is True
        final_queue = conn.execute(
            "SELECT COUNT(*) AS count FROM token_discovery_dirty_lookup_keys WHERE lookup_key = 'symbol:BULK'"
        ).fetchone()["count"]
        final_resolved = conn.execute(
            """
            SELECT COUNT(*) AS count
              FROM token_intent_resolutions
             WHERE is_current AND target_id = %s
            """,
            (f"asset:eip155:1:erc20:{ADDRESS}",),
        ).fetchone()["count"]
    finally:
        conn.close()

    assert first is True
    assert continuation["reprocess_lookup_keys"] == ["cex_token:BULK", "project_symbol:BULK", "symbol:BULK"]
    assert continuation["reprocess_after_intent_id"]
    assert continuation["reprocess_resolved"] is True
    assert continuation["attempt_count"] == 1
    assert continuation["lease_owner"] is None
    assert first_resolved == 20
    assert 26 <= turns <= 100
    assert market.search_requests == 1
    assert final_queue == 0
    assert final_resolved == 501
