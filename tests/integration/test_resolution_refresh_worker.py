from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

from tests.factories import make_event
from tests.postgres_test_utils import connect_postgres_test, repository_session_for_connection
from tests.postgres_test_utils import reset_postgres_schema as migrate
from tracefold.app.repositories import repositories_for_connection
from tracefold.market import (
    DexProviderTemporarilyUnavailable,
    DexTokenCandidate,
    DexTokenQuote,
    IngestService,
)
from tracefold.market import (
    ResolutionRefreshWorker as ProductionResolutionRefreshWorker,
)


class _InlineResources:
    async def run_background_db(self, function, /, *args, **kwargs):
        return function(*args, **kwargs)

    async def run_provider_io(self, function, /, *args, **kwargs):
        return function(*args, **kwargs)


class _InlineProviderGovernor:
    @asynccontextmanager
    async def acquire(self, *, host: str, lane: str | None = None):
        del host, lane
        yield


class ResolutionRefreshWorker(ProductionResolutionRefreshWorker):
    """Production worker with deterministic inline resources for integration tests."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.bind_runtime_resources(_InlineResources())
        self.bind_provider_governor(_InlineProviderGovernor())


def _evm_address(index: int) -> str:
    return f"0x{index:040x}"


def _dex_candidate(
    *,
    chain_id: str,
    address: str,
    symbol: str = "HANTA",
    market_cap_usd: float | None = None,
    liquidity_usd: float | None = None,
    holders: int | None = None,
    price_usd: float | None = 1.0,
) -> DexTokenCandidate:
    return DexTokenCandidate(
        chain_id=chain_id,
        address=address,
        symbol=symbol,
        name=symbol,
        price_usd=price_usd,
        market_cap_usd=market_cap_usd,
        liquidity_usd=liquidity_usd,
        holders=holders,
        community_recognized=None,
        raw={"chain_id": chain_id, "tokenContractAddress": address, "tokenSymbol": symbol},
    )


def _ingest_service_for_connection(conn) -> IngestService:
    repos = repositories_for_connection(
        conn,
    )
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


def test_resolution_refresh_worker_resolves_recent_symbol_and_defers_projection(tmp_path):
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    now_ms = 1_778_145_100_000
    address = "0x44b28991b167582f18ba0259e0173176ca125505"
    try:
        migrate(conn)
        ingest = _ingest_service_for_connection(conn)
        event = make_event(
            "event-upeg",
            text="$UPEG is getting attention",
            received_at_ms=now_ms,
        )
        ingested = ingest.ingest_event(event)
        repos = repositories_for_connection(
            conn,
        )
        before = repos.intent_resolutions.active_resolution_for_intent(ingested.token_intents[0]["intent_id"])

        worker = ResolutionRefreshWorker(
            name="resolution_refresh",
            settings=resolution_worker_settings(interval_seconds=60, chain_ids=("eip155:1",)),
            db=FakeWorkerDB(conn),
            telemetry=object(),
            dex_discovery_market=FakeDexMarket(
                candidates=[
                    DexTokenCandidate(
                        chain_id="eip155:1",
                        address=address,
                        symbol="UPEG",
                        name="Unipeg",
                        price_usd=1061.0,
                        market_cap_usd=10_600_000.0,
                        liquidity_usd=920_000.0,
                        holders=4_885,
                        community_recognized=True,
                        raw={"tokenSymbol": "UPEG"},
                    )
                ]
            ),
        )

        result = asyncio.run(worker.run_once(now_ms=now_ms + 60_000)).notes["result"]
        after = repos.intent_resolutions.active_resolution_for_intent(ingested.token_intents[0]["intent_id"])
        identity = conn.execute(
            """
            SELECT registry_assets.asset_id, registry_assets.chain_id, registry_assets.address,
                   registry_assets.status, asset_identity_current.canonical_symbol,
                   asset_identity_current.canonical_name,
                   asset_identity_current.identity_confidence
            FROM registry_assets
            JOIN asset_identity_current ON asset_identity_current.asset_id = registry_assets.asset_id
            WHERE registry_assets.asset_id = %s
            """,
            (f"asset:eip155:1:erc20:{address}",),
        ).fetchone()
        evidence = conn.execute(
            """
            SELECT *
            FROM asset_identity_evidence
            WHERE asset_id = %s
            ORDER BY observed_at_ms DESC, evidence_id DESC
            """,
            (f"asset:eip155:1:erc20:{address}",),
        ).fetchall()
        discovery = conn.execute(
            """
            SELECT status, candidate_count, candidate_ids_json
            FROM token_discovery_results
            WHERE provider = 'okx_dex_search' AND lookup_key = %s
            """,
            ("symbol:UPEG",),
        ).fetchone()
    finally:
        conn.close()

    assert before["resolution_status"] == "NIL"
    assert before["target_id"] is None
    assert result["lookups_selected"] == 1
    assert result["search_requests"] == 1
    assert result["search_hits"] == 1
    assert result["assets_written"] == 1
    assert result["reprocessed_intents"] == 1
    assert result["discovery_result_counts"] == {"found": 1}
    assert after["resolution_status"] == "UNIQUE_BY_CONTEXT"
    assert after["target_type"] == "Asset"
    assert after["target_id"] == f"asset:eip155:1:erc20:{address}"
    assert identity["canonical_symbol"] == "UPEG"
    assert identity["canonical_name"] == "Unipeg"
    assert identity["identity_confidence"] == "provider_candidate"
    assert len(evidence) == 1
    assert evidence[0]["provider"] == "okx"
    assert evidence[0]["lookup_mode"] == "symbol_search"
    assert evidence[0]["evidence_kind"] == "okx_dex_symbol_candidate"
    assert evidence[0]["raw_payload_json"]["tokenSymbol"] == "UPEG"
    assert evidence[0]["raw_payload_json"]["provider_rank"] == 0
    assert discovery["status"] == "found"
    assert discovery["candidate_count"] == 1
    assert discovery["candidate_ids_json"] == [f"asset:eip155:1:erc20:{address}"]


def test_resolution_refresh_worker_skips_symbol_lookup_after_retained_candidate_resolves_intent(tmp_path):
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    now_ms = 1_778_145_100_000
    address = "0x44b28991b167582f18ba0259e0173176ca125505"
    try:
        migrate(conn)
        ingest = _ingest_service_for_connection(conn)
        worker = ResolutionRefreshWorker(
            name="resolution_refresh",
            settings=resolution_worker_settings(interval_seconds=60, chain_ids=("eip155:1",)),
            db=FakeWorkerDB(conn),
            telemetry=object(),
            dex_discovery_market=FakeDexMarket(
                candidates=[
                    DexTokenCandidate(
                        chain_id="eip155:1",
                        address=address,
                        symbol="UPEG",
                        name="Unipeg",
                        price_usd=1061.0,
                        market_cap_usd=10_600_000.0,
                        liquidity_usd=920_000.0,
                        holders=4_885,
                        community_recognized=True,
                        raw={"tokenSymbol": "UPEG"},
                    )
                ]
            ),
        )

        first_event = make_event(
            "event-upeg-1",
            text="$UPEG is getting attention",
            received_at_ms=now_ms,
        )
        ingest.ingest_event(first_event)
        asyncio.run(worker.run_once(now_ms=now_ms + 60_000))

        second_event = make_event(
            "event-upeg-2",
            text="$UPEG again",
            received_at_ms=now_ms + 40 * 60_000,
        )
        second_ingested = ingest.ingest_event(second_event)
        repos = repositories_for_connection(
            conn,
        )
        before = repos.intent_resolutions.active_resolution_for_intent(second_ingested.token_intents[0]["intent_id"])

        result = asyncio.run(worker.run_once(now_ms=second_event.received_at_ms + 1_000)).notes["result"]
        after = repos.intent_resolutions.active_resolution_for_intent(second_ingested.token_intents[0]["intent_id"])
    finally:
        conn.close()

    assert before["resolution_status"] == "UNIQUE_BY_CONTEXT"
    assert result["lookups_selected"] == 0
    assert result["lookups_done"] == 0
    assert result["reprocessed_intents"] == 0
    assert after["resolution_status"] == "UNIQUE_BY_CONTEXT"
    assert after["target_id"] == f"asset:eip155:1:erc20:{address}"


def test_resolution_refresh_worker_retries_hot_not_found_before_default_ttl(tmp_path):
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    now_ms = 1_778_145_100_000
    address = "0x54b28991b167582f18ba0259e0173176ca125505"
    dex_market = FakeDexMarket(candidates=[])
    try:
        migrate(conn)
        ingest = _ingest_service_for_connection(conn)
        worker = ResolutionRefreshWorker(
            name="resolution_refresh",
            settings=resolution_worker_settings(interval_seconds=60, chain_ids=("eip155:1",)),
            db=FakeWorkerDB(conn),
            telemetry=object(),
            dex_discovery_market=dex_market,
        )
        ingested = ingest.ingest_event(
            make_event(
                "event-fresh-1",
                text="$FRESH is waking up",
                received_at_ms=now_ms,
            ),
        )

        first = asyncio.run(worker.run_once(now_ms=now_ms + 60_000)).notes["result"]
        before = repositories_for_connection(
            conn,
        ).intent_resolutions.active_resolution_for_intent(ingested.token_intents[0]["intent_id"])
        dex_market.candidates = [
            DexTokenCandidate(
                chain_id="eip155:1",
                address=address,
                symbol="FRESH",
                name="Fresh",
                price_usd=0.5,
                market_cap_usd=500_000.0,
                liquidity_usd=50_000.0,
                holders=500,
                community_recognized=True,
                raw={"tokenSymbol": "FRESH"},
            )
        ]

        second = asyncio.run(worker.run_once(now_ms=now_ms + 120_000)).notes["result"]
        repos = repositories_for_connection(
            conn,
        )
        after = repos.intent_resolutions.active_resolution_for_intent(ingested.token_intents[0]["intent_id"])
    finally:
        conn.close()

    assert first["lookups_done"] == 1
    assert first["search_hits"] == 0
    assert before["resolution_status"] == "NIL"
    assert second["lookups_done"] == 1
    assert second["search_hits"] == 1
    assert second["reprocessed_intents"] == 1
    assert after["resolution_status"] == "UNIQUE_BY_CONTEXT"
    assert after["target_id"] == f"asset:eip155:1:erc20:{address}"


def test_provider_outage_opens_circuit_without_spending_target_attempt(tmp_path):
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    now_ms = 1_778_145_100_000
    market = UnavailableDexMarket()
    try:
        migrate(conn)
        ingest = _ingest_service_for_connection(conn)
        ingest.ingest_event(
            make_event(
                "event-provider-outage",
                text="$OUTAGE needs discovery",
                received_at_ms=now_ms,
            )
        )
        worker = ResolutionRefreshWorker(
            name="resolution_refresh",
            settings=resolution_worker_settings(
                interval_seconds=60,
                chain_ids=("eip155:1",),
            ),
            db=FakeWorkerDB(conn),
            telemetry=object(),
            dex_discovery_market=market,
        )

        first = asyncio.run(worker.run_once(now_ms=now_ms + 60_000))
        target = conn.execute(
            """
            SELECT attempt_count, lease_owner, due_at_ms
            FROM token_discovery_dirty_lookup_keys
            WHERE provider = 'okx_dex_search'
              AND lookup_key = 'symbol:OUTAGE'
            """
        ).fetchone()
        circuit = conn.execute(
            """
            SELECT status, consecutive_failures, next_probe_at_ms
            FROM provider_circuit_state
            WHERE provider = 'okx_dex_search'
            """
        ).fetchone()
        second = asyncio.run(worker.run_once(now_ms=now_ms + 61_000))
    finally:
        conn.close()

    assert market.search_requests == 1
    assert first.failed == 0
    assert first.notes["degraded"] is True
    assert first.notes["result"]["provider_unavailable"] == 1
    assert target == {
        "attempt_count": 0,
        "lease_owner": None,
        "due_at_ms": now_ms + 90_000,
    }
    assert circuit == {
        "status": "open",
        "consecutive_failures": 1,
        "next_probe_at_ms": now_ms + 90_000,
    }
    assert second.notes["degraded"] is True
    assert second.notes["result"]["lookups_selected"] == 0
    assert market.search_requests == 1


def test_discovery_terminalize_claimed_payload_hash_deletes_active_row(tmp_path):
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    now_ms = 1_778_145_100_000
    try:
        migrate(conn)
        repos = repositories_for_connection(
            conn,
        )
        conn.execute(
            """
            INSERT INTO token_discovery_dirty_lookup_keys(
              provider, lookup_key, lookup_type, dirty_reason, payload_hash, due_at_ms,
              latest_seen_ms, intent_count, refresh_priority, leased_until_ms, lease_owner,
              attempt_count, last_error, first_dirty_at_ms, updated_at_ms
            )
            VALUES (
              'okx_dex_search', 'symbol:EMPTY', 'dex_symbol_lookup', 'test', 'sha256:test-claim', %(now_ms)s,
              %(now_ms)s, 1, 0, NULL, NULL, 0, NULL, %(now_ms)s, %(now_ms)s
            )
            """,
            {"now_ms": now_ms},
        )
        conn.commit()
        claims = repos.discovery.claim_due_lookup_keys(
            now_ms=now_ms,
            limit=1,
            lease_ms=60_000,
            running_timeout_ms=60_000,
            lease_owner="resolution_refresh",
            hot_since_ms=None,
            hot_not_found_retry_ms=None,
        )

        outcome = repos.discovery.terminalize_lookup_claims(
            claims,
            worker_name="resolution_refresh",
            final_status="error",
            final_reason="provider_error_retry_budget_exhausted",
            now_ms=now_ms,
        )
        active = conn.execute("SELECT COUNT(*) AS count FROM token_discovery_dirty_lookup_keys").fetchone()
        terminal = conn.execute(
            """
            SELECT worker_name, source_table, target_key, payload_hash, operator_action
            FROM worker_queue_terminal_events
            """
        ).fetchone()
    finally:
        conn.close()

    assert outcome == {"terminalized": 1, "deleted": 1}
    assert active["count"] == 0
    assert terminal["worker_name"] == "resolution_refresh"
    assert terminal["source_table"] == "token_discovery_dirty_lookup_keys"
    assert terminal["target_key"] == "okx_dex_search:symbol:EMPTY"
    assert terminal["payload_hash"] == "sha256:test-claim"
    assert terminal["operator_action"] is None


def test_dex_symbol_discovery_retains_top_three_per_chain(tmp_path):
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    now_ms = 1_778_145_100_000
    try:
        migrate(conn)
        ingest = _ingest_service_for_connection(conn)
        ingest.ingest_event(
            make_event("event-hanta-top3", text="$HANTA is moving", received_at_ms=now_ms),
        )
        worker = ResolutionRefreshWorker(
            name="resolution_refresh",
            settings=resolution_worker_settings(interval_seconds=60, chain_ids=("solana", "eip155:56")),
            db=FakeWorkerDB(conn),
            telemetry=object(),
            dex_discovery_market=FakeDexMarket(
                candidates=[
                    _dex_candidate(
                        chain_id="solana",
                        address="SoLow111111111111111111111111111111111111",
                        market_cap_usd=1,
                        liquidity_usd=1,
                        holders=1,
                    ),
                    _dex_candidate(
                        chain_id="solana",
                        address="SoTop111111111111111111111111111111111111",
                        market_cap_usd=1_000_000,
                        liquidity_usd=50_000,
                        holders=5_000,
                    ),
                    _dex_candidate(
                        chain_id="solana",
                        address="SoTop222222222222222222222222222222222222",
                        market_cap_usd=900_000,
                        liquidity_usd=60_000,
                        holders=4_000,
                    ),
                    _dex_candidate(
                        chain_id="solana",
                        address="SoTop333333333333333333333333333333333333",
                        market_cap_usd=800_000,
                        liquidity_usd=70_000,
                        holders=3_000,
                    ),
                    _dex_candidate(
                        chain_id="solana",
                        address="SoLow222222222222222222222222222222222222",
                        market_cap_usd=2,
                        liquidity_usd=2,
                        holders=2,
                    ),
                    _dex_candidate(
                        chain_id="solana",
                        address="SoLow333333333333333333333333333333333333",
                        market_cap_usd=3,
                        liquidity_usd=3,
                        holders=3,
                    ),
                    _dex_candidate(
                        chain_id="eip155:56",
                        address=_evm_address(1),
                        market_cap_usd=10,
                        liquidity_usd=10,
                        holders=10,
                    ),
                    _dex_candidate(
                        chain_id="eip155:56",
                        address=_evm_address(2),
                        market_cap_usd=700_000,
                        liquidity_usd=20_000,
                        holders=2_000,
                    ),
                    _dex_candidate(
                        chain_id="eip155:56",
                        address=_evm_address(3),
                        market_cap_usd=600_000,
                        liquidity_usd=30_000,
                        holders=1_500,
                    ),
                    _dex_candidate(
                        chain_id="eip155:56",
                        address=_evm_address(4),
                        market_cap_usd=500_000,
                        liquidity_usd=40_000,
                        holders=1_000,
                    ),
                ]
            ),
        )

        result = asyncio.run(worker.run_once(now_ms=now_ms + 60_000)).notes["result"]
        rows = conn.execute(
            """
            SELECT registry_assets.chain_id, registry_assets.address, registry_assets.status,
                   asset_identity_current.canonical_symbol AS symbol,
                   asset_identity_current.canonical_name AS name,
                   asset_identity_current.identity_confidence
            FROM registry_assets
            JOIN asset_identity_current ON asset_identity_current.asset_id = registry_assets.asset_id
            WHERE asset_identity_current.canonical_symbol = %s
            ORDER BY registry_assets.chain_id, registry_assets.address
            """,
            ("HANTA",),
        ).fetchall()
        discovery = conn.execute(
            """
            SELECT status, candidate_count, candidate_ids_json
            FROM token_discovery_results
            WHERE provider = 'okx_dex_search' AND lookup_key = %s
            """,
            ("symbol:HANTA",),
        ).fetchone()
    finally:
        conn.close()

    assert result["search_candidates_seen"] == 10
    assert result["search_candidates_rejected"] == 4
    assert result["assets_written"] == 6
    by_chain: dict[str, list[str]] = {}
    for row in rows:
        assert row["status"] == "candidate"
        assert row["symbol"] == "HANTA"
        assert row["name"] == "HANTA"
        assert row["identity_confidence"] == "provider_candidate"
        by_chain.setdefault(row["chain_id"], []).append(row["address"])
    assert by_chain["eip155:56"] == [_evm_address(2), _evm_address(3), _evm_address(4)]
    assert by_chain["solana"] == [
        "SoTop111111111111111111111111111111111111",
        "SoTop222222222222222222222222222222222222",
        "SoTop333333333333333333333333333333333333",
    ]
    assert discovery["status"] == "found"
    assert discovery["candidate_count"] == 6
    assert discovery["candidate_ids_json"] == sorted(
        [
            f"asset:eip155:56:erc20:{_evm_address(2)}",
            f"asset:eip155:56:erc20:{_evm_address(3)}",
            f"asset:eip155:56:erc20:{_evm_address(4)}",
            "asset:solana:token:SoTop111111111111111111111111111111111111",
            "asset:solana:token:SoTop222222222222222222222222222222222222",
            "asset:solana:token:SoTop333333333333333333333333333333333333",
        ]
    )


def test_dex_symbol_discovery_excludes_stale_unretained_search_assets_from_result(tmp_path):
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    now_ms = 1_778_145_100_000
    try:
        migrate(conn)
        repos = repositories_for_connection(
            conn,
        )
        ingest = _ingest_service_for_connection(conn)
        ingest.ingest_event(
            make_event("event-hanta-demote", text="$HANTA", received_at_ms=now_ms + 1_000),
        )
        old = repos.registry.upsert_chain_asset(
            chain_id="eip155:56",
            address=_evm_address(99),
            observed_at_ms=now_ms,
        )
        repos.identity_evidence.upsert_identity_evidence(
            asset_id=old["asset_id"],
            evidence_kind="okx_dex_symbol_candidate",
            provider="okx",
            lookup_mode="symbol_search",
            chain_id=old["chain_id"],
            address=old["address"],
            symbol="HANTA",
            name="Old HANTA",
            decimals=18,
            confidence="provider_candidate",
            raw_payload={"tokenSymbol": "HANTA", "stale": True},
            observed_at_ms=now_ms,
        )
        repos.identity_evidence.recompute_current_identity(
            old["asset_id"],
            now_ms=now_ms,
        )
        conn.commit()
        worker = ResolutionRefreshWorker(
            name="resolution_refresh",
            settings=resolution_worker_settings(interval_seconds=60, chain_ids=("eip155:56",)),
            db=FakeWorkerDB(conn),
            telemetry=object(),
            dex_discovery_market=FakeDexMarket(
                candidates=[
                    _dex_candidate(
                        chain_id="eip155:56",
                        address=_evm_address(1),
                        market_cap_usd=1_000_000,
                        liquidity_usd=10_000,
                        holders=1_000,
                    ),
                    _dex_candidate(
                        chain_id="eip155:56",
                        address=_evm_address(2),
                        market_cap_usd=900_000,
                        liquidity_usd=10_000,
                        holders=900,
                    ),
                    _dex_candidate(
                        chain_id="eip155:56",
                        address=_evm_address(3),
                        market_cap_usd=800_000,
                        liquidity_usd=10_000,
                        holders=800,
                    ),
                ]
            ),
        )

        asyncio.run(worker.run_once(now_ms=now_ms + 60_000))
        row = conn.execute("SELECT status FROM registry_assets WHERE asset_id = %s", (old["asset_id"],)).fetchone()
        discovery = conn.execute(
            """
            SELECT status, candidate_count, candidate_ids_json
            FROM token_discovery_results
            WHERE provider = 'okx_dex_search' AND lookup_key = %s
            """,
            ("symbol:HANTA",),
        ).fetchone()
    finally:
        conn.close()

    expected_ids = sorted(
        [
            f"asset:eip155:56:erc20:{_evm_address(1)}",
            f"asset:eip155:56:erc20:{_evm_address(2)}",
            f"asset:eip155:56:erc20:{_evm_address(3)}",
        ]
    )
    assert row["status"] == "candidate"
    assert discovery["status"] == "found"
    assert discovery["candidate_count"] == 3
    assert discovery["candidate_ids_json"] == expected_ids
    assert old["asset_id"] not in discovery["candidate_ids_json"]


def resolution_worker_settings(**overrides):
    values = {
        "enabled": True,
        "interval_seconds": 30.0,
        "timeout_seconds": 120.0,
        "batch_size": 50,
        "lease_ms": 300_000,
        "hot_not_found_retry_ms": 60_000,
        "reprocess_limit": 500,
        "max_attempts": 3,
        "chain_ids": ("solana", "eip155:1", "eip155:56", "eip155:8453", "ton"),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class FakeWorkerDB:
    def __init__(self, conn):
        self.conn = conn
        self.session_names: list[str] = []

    def worker_session(self, name: str):
        self.session_names.append(name)
        return repository_session_for_connection(self.conn)


class FakeDexMarket:
    def __init__(self, *, candidates):
        self.candidates = candidates
        self.search_requests = []
        self.price_requests = []

    def search_tokens(self, *, query, chain_ids):
        self.search_requests.append({"query": query, "chain_ids": tuple(chain_ids)})
        return list(self.candidates)

    def token_quotes(self, requests):
        self.price_requests.append(list(requests))
        prices = []
        for request in requests:
            prices.extend(
                DexTokenQuote(
                    chain_id=candidate.chain_id,
                    address=candidate.address,
                    observed_at_ms=1_778_145_220_000,
                    price_usd=candidate.price_usd,
                    market_cap_usd=candidate.market_cap_usd,
                    liquidity_usd=candidate.liquidity_usd,
                    holders=candidate.holders,
                    raw={"priceUsd": candidate.price_usd},
                )
                for candidate in self.candidates
                if candidate.chain_id == request.chain_id and candidate.address.lower() == request.address.lower()
            )
        return prices


class UnavailableDexMarket:
    def __init__(self) -> None:
        self.search_requests = 0

    def search_tokens(self, *, query, chain_ids):
        del query, chain_ids
        self.search_requests += 1
        raise DexProviderTemporarilyUnavailable("provider unavailable")
