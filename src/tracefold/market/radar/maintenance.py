from __future__ import annotations

from typing import Any
from uuid import uuid4

from tracefold.market.radar.constants import (
    TOKEN_RADAR_RESOLVER_POLICY_VERSION,
)
from tracefold.market.radar.microbatch import (
    RadarMicroBatchService,
    compute_radar_target_batch,
    hydrate_radar_microbatch,
    rank_radar_microbatch,
)

_EVENT_BATCH_SIZE = 1_000
_MICROBATCH_CAP = 1_000_000


def rebuild_all_token_radar_for_maintenance(
    *,
    db: Any,
    now_ms: int,
) -> dict[str, Any]:
    """Rebuild Radar from facts through the same bounded micro-batch reducer."""

    service = RadarMicroBatchService(
        db=db,
        worker_name="radar_maintenance_rebuild",
    )
    with service._session() as repos, repos.transaction():
        reset_counts = {
            "current_rows": int(repos.conn.execute("DELETE FROM token_radar_current_rows").rowcount or 0),
            "publication_states": int(repos.conn.execute("DELETE FROM token_radar_publication_state").rowcount or 0),
            "target_features": int(repos.conn.execute("DELETE FROM token_radar_target_features").rowcount or 0),
            "stocks_current_rows": int(repos.conn.execute("DELETE FROM stocks_radar_current_rows").rowcount or 0),
            "stocks_publication_states": int(
                repos.conn.execute("DELETE FROM stocks_radar_publication_state").rowcount or 0
            ),
            "stocks_target_features": int(
                repos.conn.execute("DELETE FROM stock_attention_target_features").rowcount or 0
            ),
            "source_edges": int(repos.conn.execute("DELETE FROM radar_source_edges").rowcount or 0),
            "frontiers": int(repos.conn.execute("DELETE FROM radar_projection_frontiers").rowcount or 0),
        }

    event_count = 0
    edge_writes = 0
    after: tuple[int, str] | None = None
    cutoff_ms = int(now_ms) - 48 * 60 * 60 * 1_000
    while True:
        after_predicate = (
            """
              AND (event.received_at_ms, event.event_id)
                  > (%(after_received_at_ms)s, %(after_event_id)s)
            """
            if after is not None
            else ""
        )
        with service._session() as repos:
            rows = repos.conn.execute(
                f"""
                SELECT DISTINCT event.received_at_ms, event.event_id
                FROM events event
                JOIN token_intents intent
                  ON intent.event_id = event.event_id
                JOIN token_intent_resolutions resolution
                  ON resolution.intent_id = intent.intent_id
                 AND resolution.event_id = event.event_id
                WHERE event.received_at_ms >= %(cutoff_ms)s
                  AND resolution.is_current
                  AND (
                    resolution.target_type IN ('Asset', 'CexToken')
                    OR (
                      resolution.target_type = 'MarketInstrument'
                      AND resolution.resolution_status = 'NON_CRYPTO'
                      AND resolution.resolver_policy_version =
                            %(resolver_policy_version)s
                      AND resolution.reason_codes_json
                            @> '["CONFIRMED_US_EQUITY"]'::jsonb
                    )
                  )
                  AND resolution.target_id IS NOT NULL
                  {after_predicate}
                ORDER BY event.received_at_ms, event.event_id
                LIMIT %(limit)s
                """,
                {
                    "cutoff_ms": cutoff_ms,
                    "after_received_at_ms": after[0] if after else None,
                    "after_event_id": after[1] if after else None,
                    "limit": _EVENT_BATCH_SIZE,
                    "resolver_policy_version": (TOKEN_RADAR_RESOLVER_POLICY_VERSION),
                },
            ).fetchall()
        if not rows:
            break
        for row in rows:
            with service._session() as repos, repos.transaction():
                edge_writes += repos.radar_source_edges.sync_event(
                    event_id=str(row["event_id"]),
                    now_ms=int(now_ms),
                )
            event_count += 1
        last = rows[-1]
        after = (int(last["received_at_ms"]), str(last["event_id"]))

    runtime_id = str(uuid4())
    results: list[dict[str, Any]] = []
    while True:
        due = service.next_due(now_ms=int(now_ms))
        if due is None:
            break
        if len(results) >= _MICROBATCH_CAP:
            raise RuntimeError("radar_maintenance_microbatch_cap_exceeded")
        claim = service.claim_batch(
            window=str(due["window_key"]),
            venue=str(due["venue"]),
            runtime_id=runtime_id,
            now_ms=int(now_ms),
        )
        if claim is None:
            raise RuntimeError("radar_maintenance_claim_missing")
        loaded = service.load_targets(claim, now_ms=int(now_ms))
        projections = compute_radar_target_batch(loaded)
        rank_inputs = service.load_rank_inputs(
            claim,
            projections=projections,
            now_ms=int(now_ms),
        )
        ranked = rank_radar_microbatch(rank_inputs)
        hydrated = service.load_hydration(claim, ranked=ranked)
        result = service.publish(
            claim,
            projections=projections,
            ranked=ranked,
            closure=hydrate_radar_microbatch(
                ranked=ranked,
                hydrated_inputs=hydrated,
            ),
            now_ms=int(now_ms),
        )
        if result["projection_status"] not in {"published", "unchanged"}:
            raise RuntimeError(f"radar_maintenance_publish_failed:{result['projection_status']}")
        results.append(result)

    with service._session() as repos:
        quarantined = int(
            repos.conn.execute(
                """
                SELECT count(*) AS count
                FROM radar_projection_frontiers
                WHERE status = 'quarantined'
                """
            ).fetchone()["count"]
        )
        current_rows = int(
            repos.conn.execute("SELECT count(*) AS count FROM token_radar_current_rows").fetchone()["count"]
        )
        stocks_current_rows = int(
            repos.conn.execute("SELECT count(*) AS count FROM stocks_radar_current_rows").fetchone()["count"]
        )
    if quarantined:
        raise RuntimeError(f"radar_maintenance_quarantine_unresolved:{quarantined}")
    return {
        "projection_status": "rebuilt",
        "events_scanned": event_count,
        "source_edges_written": edge_writes,
        "microbatches_computed": len(results),
        "targets_computed": sum(int(result["targets_loaded"]) for result in results),
        "rows_written": sum(int(result["rows_written"]) for result in results),
        "current_rows": current_rows,
        "stocks_current_rows": stocks_current_rows,
        "reset": reset_counts,
    }


__all__ = ["rebuild_all_token_radar_for_maintenance"]
