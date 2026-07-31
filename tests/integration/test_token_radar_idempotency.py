from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from uuid import uuid4

from psycopg import pq
from psycopg.types.json import Jsonb

from tests.factories import make_event
from tests.postgres_test_utils import (
    connect_postgres_test,
    repository_session_for_connection,
)
from tests.postgres_test_utils import reset_postgres_schema as migrate
from tracefold.app.repositories import repositories_for_connection
from tracefold.market import (
    TOKEN_RADAR_PROJECTION_VERSION,
    TOKEN_RADAR_RESOLVER_POLICY_VERSION,
    EvidenceRepository,
    RegistryRepository,
)
from tracefold.market.radar.microbatch import (
    RadarMicroBatchService,
    compute_radar_target_batch,
    hydrate_radar_microbatch,
    rank_radar_microbatch,
)

FIXED_NOW_MS = 1_800_000_000_000
EVENT_MS = FIXED_NOW_MS - 10 * 60 * 1000
ASSET_ADDRESS = "0x1111111111111111111111111111111111111111"


class _SingleConnectionDB:
    def __init__(self, conn: Any) -> None:
        self.conn = conn

    @contextmanager
    def worker_session(
        self,
        *_args: Any,
        **_kwargs: Any,
    ) -> Iterator[Any]:
        try:
            with repository_session_for_connection(self.conn) as repos:
                yield repos
        finally:
            if self.conn.info.transaction_status != pq.TransactionStatus.IDLE:
                self.conn.rollback()


def test_token_radar_incremental_projection_is_idempotent(tmp_path):
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        _seed_resolved_radar_source(conn)
        conn.commit()

        repos = repositories_for_connection(conn)
        with repos.transaction():
            first_enqueued = repos.radar_source_edges.sync_event(
                event_id="event-radar-idempotent",
                now_ms=EVENT_MS,
            )
        first_result = _run_radar_projection(
            conn,
            window="1h",
            now_ms=FIXED_NOW_MS,
        )
        first_rows = _radar_rows(conn)

        with repos.transaction():
            second_enqueued = repos.radar_source_edges.sync_event(
                event_id="event-radar-idempotent",
                now_ms=EVENT_MS,
            )
        second_result = _run_radar_projection(
            conn,
            window="1h",
            now_ms=FIXED_NOW_MS,
        )
        second_rows = _radar_rows(conn)
    finally:
        conn.close()

    assert first_result["projection_status"] == "published"
    assert second_result["projection_status"] == "idle"
    assert first_enqueued == 4
    assert second_enqueued == 0
    assert first_result["rows_written"] >= 1
    assert second_result["rows_written"] == 0
    assert first_rows, "seeded current facts should produce at least one radar row"
    assert _semantic_rows(first_rows) == _semantic_rows(second_rows)


def _run_radar_projection(
    conn: Any,
    *,
    window: str,
    now_ms: int,
    venue: str = "all",
) -> dict[str, Any]:
    conn.commit()
    service = RadarMicroBatchService(db=_SingleConnectionDB(conn))
    claim = service.claim_batch(
        window=window,
        venue=venue,
        runtime_id=str(uuid4()),
        now_ms=now_ms,
    )
    if claim is None:
        return {"projection_status": "idle", "rows_written": 0}
    loaded = service.load_targets(claim, now_ms=now_ms)
    projections = compute_radar_target_batch(loaded)
    rank_inputs = service.load_rank_inputs(
        claim,
        projections=projections,
        now_ms=now_ms,
    )
    ranked = rank_radar_microbatch(rank_inputs)
    hydrated = service.load_hydration(claim, ranked=ranked)
    closure = hydrate_radar_microbatch(
        ranked=ranked,
        hydrated_inputs=hydrated,
    )
    return service.publish(
        claim,
        projections=projections,
        ranked=ranked,
        closure=closure,
        now_ms=now_ms,
    )


def _radar_rows(conn: Any) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
          lane,
          rank,
          intent_id,
          event_id,
          target_type,
          target_id,
          decision,
          factor_snapshot_json,
          data_health_json,
          source_event_ids_json
        FROM token_radar_current_rows
        WHERE projection_version = %s
          AND "window" = '1h'
        ORDER BY lane, rank, target_type, target_id, intent_id
        """,
        (TOKEN_RADAR_PROJECTION_VERSION,),
    ).fetchall()
    return [dict(row) for row in rows]


def _semantic_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [_json_stable(row) for row in rows]


def _json_stable(value: Any) -> Any:
    return json.loads(json.dumps(value, sort_keys=True, default=str))


def _seed_resolved_radar_source(conn: Any) -> None:
    event_id = "event-radar-idempotent"
    intent_id = "intent-radar-idempotent"
    resolution_id = "resolution-radar-idempotent"

    with conn.transaction():
        EvidenceRepository(conn).insert_event(
            make_event(
                event_id=event_id,
                author_handle="signal_builder",
                text="$IDEMP fresh onchain momentum",
                received_at_ms=EVENT_MS,
            ),
        )
    _insert_intent(conn, intent_id=intent_id, event_id=event_id, observed_at_ms=EVENT_MS)
    asset_id = _insert_asset(conn, observed_at_ms=EVENT_MS)
    _insert_current_identity(conn, asset_id=asset_id, observed_at_ms=EVENT_MS)
    _insert_resolution(
        conn,
        resolution_id=resolution_id,
        intent_id=intent_id,
        event_id=event_id,
        asset_id=asset_id,
        observed_at_ms=EVENT_MS,
    )
    _insert_market_tick(
        conn,
        tick_id="tick-radar-idempotent-anchor",
        observed_at_ms=EVENT_MS,
        received_at_ms=EVENT_MS,
    )
    _insert_market_tick(
        conn,
        tick_id="tick-radar-idempotent-latest",
        observed_at_ms=FIXED_NOW_MS - 30_000,
        received_at_ms=FIXED_NOW_MS - 30_000,
    )
    _insert_enriched_event(
        conn,
        event_id=event_id,
        intent_id=intent_id,
        resolution_id=resolution_id,
        tick_id="tick-radar-idempotent-anchor",
        tick_observed_at_ms=EVENT_MS,
        t_event_ms=EVENT_MS,
    )


def _insert_asset(conn: Any, *, observed_at_ms: int) -> str:
    asset = RegistryRepository(conn).upsert_chain_asset(
        chain_id="eip155:1",
        address=ASSET_ADDRESS,
        observed_at_ms=observed_at_ms,
        status="candidate",
    )
    return str(asset["asset_id"])


def _insert_intent(conn: Any, *, intent_id: str, event_id: str, observed_at_ms: int) -> None:
    conn.execute(
        """
        INSERT INTO token_intents(
          intent_id, event_id, intent_key, construction_policy, display_symbol,
          display_name, intent_status, intent_confidence, created_at_ms, updated_at_ms
        )
        VALUES (%s, %s, 'symbol:IDEMP', 'integration-test', 'IDEMP',
                'Idempotency Token', 'active', 1.0, %s, %s)
        """,
        (intent_id, event_id, observed_at_ms, observed_at_ms),
    )


def _insert_current_identity(conn: Any, *, asset_id: str, observed_at_ms: int) -> None:
    conn.execute(
        """
        INSERT INTO asset_identity_current(
          asset_id, canonical_symbol, canonical_name, decimals, identity_confidence,
          selection_reason_codes_json, conflict_count, verified_at_ms, updated_at_ms
        )
        VALUES (%s, 'IDEMP', 'Idempotency Token', 18, 'provider_exact',
                %s, 0, %s, %s)
        """,
        (asset_id, Jsonb(["SELECTED_PROVIDER_EXACT"]), observed_at_ms, observed_at_ms),
    )


def _insert_resolution(
    conn: Any,
    *,
    resolution_id: str,
    intent_id: str,
    event_id: str,
    asset_id: str,
    observed_at_ms: int,
) -> None:
    conn.execute(
        """
        INSERT INTO token_intent_resolutions(
          resolution_id, intent_id, event_id, resolution_status, resolver_policy_version,
          target_type, target_id, pricefeed_id, reason_codes_json, candidate_ids_json,
          lookup_keys_json, record_status, is_current, decision_time_ms, created_at_ms
        )
        VALUES (
          %s, %s, %s, 'UNIQUE_BY_CONTEXT', %s,
          'Asset', %s, NULL, %s, %s, %s,
          'current', true, %s, %s
        )
        """,
        (
            resolution_id,
            intent_id,
            event_id,
            TOKEN_RADAR_RESOLVER_POLICY_VERSION,
            asset_id,
            Jsonb(["INTEGRATION_TEST"]),
            Jsonb([asset_id]),
            Jsonb(["symbol:IDEMP", f"address:eip155:1:{ASSET_ADDRESS.lower()}"]),
            observed_at_ms,
            observed_at_ms,
        ),
    )


def _insert_market_tick(conn: Any, *, tick_id: str, observed_at_ms: int, received_at_ms: int) -> None:
    target_id = f"eip155:1:{ASSET_ADDRESS.lower()}"
    conn.execute(
        """
        INSERT INTO market_ticks(
          tick_id, target_type, target_id, chain, token_address,
          exchange, instrument, pricefeed_id, source_tier, source_provider,
          observed_at_ms, received_at_ms, price_usd, liquidity_usd,
          volume_24h_usd, market_cap_usd, holders, raw_payload_json, created_at_ms
        )
        VALUES (
          %s, 'chain_token', %s, 'eip155:1', %s,
          NULL, NULL, NULL, 'tier3_inline', 'okx_dex_rest',
          %s, %s, 1.25, 100000, 500000, 1000000, 1000, %s, %s
        )
        """,
        (tick_id, target_id, ASSET_ADDRESS.lower(), observed_at_ms, received_at_ms, Jsonb({}), received_at_ms),
    )


def _insert_enriched_event(
    conn: Any,
    *,
    event_id: str,
    intent_id: str,
    resolution_id: str,
    tick_id: str,
    tick_observed_at_ms: int,
    t_event_ms: int,
) -> None:
    target_id = f"eip155:1:{ASSET_ADDRESS.lower()}"
    conn.execute(
        """
        INSERT INTO enriched_events(
          event_id, intent_id, resolution_id, target_type, target_id,
          t_event_ms, tick_observed_at_ms, tick_id, tick_lag_ms, capture_method, capture_reason, created_at_ms
        )
        VALUES (
          %s, %s, %s, 'chain_token', %s,
          %s, %s, %s, 0, 'tier3_inline', 'integration_seed', %s
        )
        """,
        (event_id, intent_id, resolution_id, target_id, t_event_ms, tick_observed_at_ms, tick_id, t_event_ms),
    )
