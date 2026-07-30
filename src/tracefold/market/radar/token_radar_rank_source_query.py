from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from psycopg.types.json import Jsonb

from tracefold.platform.validation import require_positive_int

TOKEN_RADAR_RANK_SOURCE_REQUEST_CHUNK_SIZE = 200


@dataclass(frozen=True)
class TokenRadarFeatureSourceRequest:
    request_key: str
    target_type_key: str
    identity_id: str
    window: str
    analysis_since_ms: int
    score_since_ms: int
    now_ms: int


class TokenRadarRankSourceQuery:
    def __init__(self, conn: Any, *, chunk_size: int = TOKEN_RADAR_RANK_SOURCE_REQUEST_CHUNK_SIZE) -> None:
        self.conn = conn
        self.chunk_size = require_positive_int(
            chunk_size,
            error_code="token_radar_rank_source_chunk_size_required",
        )

    def load_rows_for_requests(
        self,
        requests: Sequence[TokenRadarFeatureSourceRequest],
        *,
        row_cap: int | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        parsed_cap = (
            require_positive_int(
                row_cap,
                error_code="token_radar_rank_source_row_cap_required",
            )
            if row_cap is not None
            else None
        )
        rows_by_request: dict[str, list[dict[str, Any]]] = {str(request.request_key): [] for request in requests}
        for chunk in _chunks(tuple(requests), self.chunk_size):
            sql = _RANK_SOURCE_ROWS_FOR_REQUESTS_SQL
            params: tuple[Any, ...] = (Jsonb([_feature_request_payload(r) for r in chunk]),)
            if parsed_cap is not None:
                sql = f"{sql}\nLIMIT %s"
                params = (*params, parsed_cap + 1)
            rows = self.conn.execute(
                sql,
                params,
            ).fetchall()
            if parsed_cap is not None and len(rows) > parsed_cap:
                raise RuntimeError("token_radar_rank_source_shard_oversized")
            for row in rows:
                payload = dict(row)
                request_key = str(payload.get("request_key") or "")
                rows_by_request.setdefault(request_key, []).append(payload)
        return rows_by_request

    def latest_market_context_for_targets(
        self,
        targets: Sequence[Mapping[str, Any]],
    ) -> dict[tuple[str, str], dict[str, Any]]:
        rows_by_target: dict[tuple[str, str], dict[str, Any]] = {}
        target_payloads = _target_payloads(targets)
        if not target_payloads:
            return rows_by_target
        rows = self.conn.execute(
            _LATEST_MARKET_CONTEXT_FOR_TARGETS_SQL,
            (Jsonb(target_payloads),),
        ).fetchall()
        for row in rows:
            payload = dict(row)
            target_key = (
                _required_target_payload_text(payload, "target_type_key"),
                _required_target_payload_text(payload, "identity_id"),
            )
            rows_by_target[target_key] = payload
        return rows_by_target


def _chunks(
    requests: tuple[TokenRadarFeatureSourceRequest, ...],
    chunk_size: int,
) -> Sequence[tuple[TokenRadarFeatureSourceRequest, ...]]:
    return tuple(requests[index : index + chunk_size] for index in range(0, len(requests), chunk_size))


def _feature_request_payload(request: TokenRadarFeatureSourceRequest) -> dict[str, Any]:
    return {
        "request_key": str(request.request_key),
        "target_type_key": str(request.target_type_key),
        "identity_id": str(request.identity_id),
        "window": str(request.window),
        "analysis_since_ms": int(request.analysis_since_ms),
        "score_since_ms": int(request.score_since_ms),
        "now_ms": int(request.now_ms),
    }


def _target_payloads(targets: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    payloads: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for target in targets:
        target_type_key = _required_target_payload_text(target, "target_type_key")
        identity_id = _required_target_payload_text(target, "identity_id")
        target_key = (target_type_key, identity_id)
        if target_key in seen:
            continue
        seen.add(target_key)
        payloads.append({"target_type_key": target_type_key, "identity_id": identity_id})
    return payloads


def _required_target_payload_text(target: Mapping[str, Any], column: str) -> str:
    try:
        value = target[column]
    except KeyError as exc:
        raise ValueError(f"token_radar_rank_source_target_identity_required:{column}") from exc
    if value is None:
        raise ValueError(f"token_radar_rank_source_target_identity_required:{column}")
    text = str(value).strip()
    if not text:
        raise ValueError(f"token_radar_rank_source_target_identity_required:{column}")
    return text


_LATEST_MARKET_CONTEXT_FOR_TARGETS_SQL = """
WITH requested AS (
  SELECT DISTINCT target_type_key, identity_id
  FROM jsonb_to_recordset(%s::jsonb) AS r(
    target_type_key text,
    identity_id text
  )
),
asset_market AS (
  SELECT
    requested.target_type_key,
    requested.identity_id,
    current_row.tick_id AS latest_price_tick_id,
    current_row.source_provider AS latest_price_provider,
    current_row.source_tier AS latest_price_source_tier,
    current_row.pricefeed_id AS latest_price_pricefeed_id,
    current_row.tick_observed_at_ms AS latest_price_observed_at_ms,
    current_row.updated_at_ms AS latest_price_received_at_ms,
    current_row.price_usd AS latest_price_usd,
    NULL::numeric AS latest_price_quote,
    NULL::text AS latest_price_quote_symbol,
    NULL::text AS latest_price_basis,
    current_row.market_cap_usd AS latest_price_market_cap_usd,
    current_row.liquidity_usd AS latest_price_liquidity_usd,
    current_row.volume_24h_usd AS latest_price_volume_24h_usd,
    current_row.open_interest_usd AS latest_price_open_interest_usd,
    current_row.holders AS latest_price_holders
  FROM requested
  JOIN registry_assets
    ON requested.target_type_key = 'Asset'
   AND registry_assets.asset_id = requested.identity_id
  JOIN market_tick_current current_row
    ON current_row.target_type = 'chain_token'
   AND current_row.target_id = registry_assets.chain_id || ':' || registry_assets.address
),
cex_market AS (
  SELECT
    requested.target_type_key,
    requested.identity_id,
    current_row.tick_id AS latest_price_tick_id,
    current_row.source_provider AS latest_price_provider,
    current_row.source_tier AS latest_price_source_tier,
    current_row.pricefeed_id AS latest_price_pricefeed_id,
    current_row.tick_observed_at_ms AS latest_price_observed_at_ms,
    current_row.updated_at_ms AS latest_price_received_at_ms,
    current_row.price_usd AS latest_price_usd,
    NULL::numeric AS latest_price_quote,
    NULL::text AS latest_price_quote_symbol,
    NULL::text AS latest_price_basis,
    current_row.market_cap_usd AS latest_price_market_cap_usd,
    current_row.liquidity_usd AS latest_price_liquidity_usd,
    current_row.volume_24h_usd AS latest_price_volume_24h_usd,
    current_row.open_interest_usd AS latest_price_open_interest_usd,
    current_row.holders AS latest_price_holders
  FROM requested
  JOIN LATERAL (
    SELECT *
    FROM price_feeds
    WHERE requested.target_type_key = 'CexToken'
      AND price_feeds.subject_type = 'CexToken'
      AND price_feeds.subject_id = requested.identity_id
      AND price_feeds.provider = 'binance'
      AND price_feeds.feed_type = 'cex_swap'
      AND price_feeds.quote_symbol = 'USDT'
      AND price_feeds.status = 'canonical'
    ORDER BY price_feeds.updated_at_ms DESC, price_feeds.native_market_id ASC
    LIMIT 1
  ) price_feeds ON true
  JOIN market_tick_current current_row
    ON current_row.target_type = 'cex_symbol'
   AND current_row.target_id = price_feeds.provider || ':' || price_feeds.native_market_id
)
SELECT *
FROM asset_market
UNION ALL
SELECT *
FROM cex_market
"""


_RANK_SOURCE_ROWS_FOR_REQUESTS_SQL = """
WITH requested AS (
  SELECT *
  FROM jsonb_to_recordset(%s::jsonb) AS r(
    request_key text,
    target_type_key text,
    identity_id text,
    "window" text,
    analysis_since_ms bigint,
    score_since_ms bigint,
    now_ms bigint
  )
),
source_edges AS (
  SELECT
    requested.request_key,
    requested."window",
    requested.score_since_ms,
    rank_source.input_fingerprint AS source_payload_hash,
    rank_source.payload_json ->> 'intent_id' AS intent_id,
    rank_source.payload_json ->> 'event_id' AS event_id,
    rank_source.payload_json ->> 'resolution_id' AS resolution_id,
    rank_source.target_type,
    rank_source.target_id,
    rank_source.payload_json ->> 'pricefeed_id' AS pricefeed_id,
    rank_source.payload_json ->> 'resolution_status' AS resolution_status,
    rank_source.observed_at_ms AS event_received_at_ms,
    row_number() OVER (
      PARTITION BY requested.request_key, rank_source.target_type, rank_source.target_id
      ORDER BY rank_source.observed_at_ms ASC, rank_source.source_id ASC
    ) - 1 AS source_rank
  FROM requested
  JOIN radar_source_edges rank_source
    ON rank_source.target_type = requested.target_type_key
   AND rank_source.target_id = requested.identity_id
   AND rank_source.window_key = requested."window"
   AND rank_source.venue = 'all'
  WHERE rank_source.source_kind = 'event'
    AND rank_source.observed_at_ms >= requested.analysis_since_ms
    AND rank_source.observed_at_ms <= requested.now_ms
    AND rank_source.expires_at_ms > requested.now_ms
),
hydrated AS (
  SELECT
    source_edges.request_key,
    source_edges."window",
    source_edges.score_since_ms,
    source_edges.source_payload_hash,
    source_edges.intent_id,
    source_edges.event_id,
    token_intents.intent_key,
    token_intents.construction_policy,
    token_intents.primary_evidence_id,
    token_intents.display_symbol,
    token_intents.display_name,
    token_intents.chain_hint,
    token_intents.address_hint,
    token_intents.intent_status,
    token_intents.created_at_ms,
    token_intents.updated_at_ms,
    source_edges.resolution_id,
    source_edges.target_type,
    source_edges.target_id,
    CASE
      WHEN source_edges.target_type = 'CexToken' THEN preferred_price_feed.pricefeed_id
      ELSE source_edges.pricefeed_id
    END AS pricefeed_id,
    source_edges.resolution_status,
    token_intent_resolutions.reason_codes_json,
    token_intent_resolutions.candidate_ids_json,
    token_intent_resolutions.lookup_keys_json,
    token_intent_resolutions.decision_time_ms,
    events.author_handle,
    source_edges.event_received_at_ms AS received_at_ms,
    source_edges.source_rank,
    md5(COALESCE(events.search_tsv::text, '')) AS text_fingerprint,
    CASE
      WHEN events.search_tsv IS NULL THEN NULL
      WHEN events.search_tsv @@ websearch_to_tsquery('simple', 'price OR liquidity OR volume OR holders OR mcap OR fdv')
        THEN 80
      ELSE 55
    END AS post_quality_score,
    CASE WHEN events.search_tsv IS NULL THEN NULL ELSE true END AS post_informative,
    CASE
      WHEN events.search_tsv IS NULL THEN NULL
      ELSE events.search_tsv @@ websearch_to_tsquery('simple', 'price OR liquidity OR volume OR holders OR mcap OR fdv')
    END AS post_has_market_context,
    events.author_followers,
    events.author_tags_json,
    registry_assets.chain_id AS asset_chain_id,
    registry_assets.token_standard AS asset_token_standard,
    registry_assets.address AS asset_address,
    asset_identity_current.canonical_symbol AS asset_symbol,
    asset_identity_current.canonical_name AS asset_name,
    asset_identity_current.identity_confidence AS asset_identity_confidence,
    asset_identity_current.selection_reason_codes_json AS asset_identity_reason_codes,
    asset_identity_current.conflict_count AS asset_identity_conflict_count,
    registry_assets.status AS asset_registry_status,
    cex_tokens.base_symbol AS cex_base_symbol,
    cex_tokens.status AS cex_token_status,
    price_feeds.feed_type,
    price_feeds.provider AS pricefeed_provider,
    price_feeds.native_market_id,
    price_feeds.base_symbol AS pricefeed_base_symbol,
    price_feeds.quote_symbol AS pricefeed_quote_symbol,
    price_feeds.status AS pricefeed_status,
    NULL::bigint AS first_price_observed_at_ms,
    NULL::numeric AS first_price_usd,
    NULL::numeric AS first_price_quote,
    NULL::text AS first_price_quote_symbol,
    NULL::text AS first_price_basis,
    CASE WHEN event_price_tick.tick_id IS NOT NULL THEN event_price_capture.tick_id ELSE NULL END
      AS event_price_capture_id,
    CASE WHEN event_price_tick.tick_id IS NOT NULL THEN event_price_capture.capture_method ELSE NULL END
      AS event_price_capture_method,
    CASE WHEN event_price_tick.tick_id IS NOT NULL THEN event_price_capture.capture_reason ELSE NULL END
      AS event_price_capture_reason,
    CASE WHEN event_price_tick.tick_id IS NOT NULL THEN event_price_capture.tick_lag_ms ELSE NULL END
      AS event_price_tick_lag_ms,
    event_price_tick.source_provider AS event_price_provider,
    event_price_tick.source_tier AS event_price_source_tier,
    event_price_tick.pricefeed_id AS event_price_pricefeed_id,
    event_price_tick.observed_at_ms AS event_price_observed_at_ms,
    event_price_tick.created_at_ms AS event_price_received_at_ms,
    event_price_tick.price_usd AS event_price_usd,
    NULL::numeric AS event_price_quote,
    NULL::text AS event_price_quote_symbol,
    NULL::text AS event_price_basis,
    event_price_tick.market_cap_usd AS event_price_market_cap_usd,
    event_price_tick.liquidity_usd AS event_price_liquidity_usd,
    event_price_tick.volume_24h_usd AS event_price_volume_24h_usd,
    event_price_tick.open_interest_usd AS event_price_open_interest_usd,
    event_price_tick.holders AS event_price_holders,
    latest_price_tick.tick_id AS latest_price_tick_id,
    latest_price_tick.source_provider AS latest_price_provider,
    latest_price_tick.source_tier AS latest_price_source_tier,
    latest_price_tick.pricefeed_id AS latest_price_pricefeed_id,
    latest_price_tick.tick_observed_at_ms AS latest_price_observed_at_ms,
    latest_price_tick.updated_at_ms AS latest_price_received_at_ms,
    latest_price_tick.price_usd AS latest_price_usd,
    NULL::numeric AS latest_price_quote,
    NULL::text AS latest_price_quote_symbol,
    NULL::text AS latest_price_basis,
    latest_price_tick.market_cap_usd AS latest_price_market_cap_usd,
    latest_price_tick.liquidity_usd AS latest_price_liquidity_usd,
    latest_price_tick.volume_24h_usd AS latest_price_volume_24h_usd,
    latest_price_tick.open_interest_usd AS latest_price_open_interest_usd,
    latest_price_tick.holders AS latest_price_holders,
    NULL::bigint AS before_event_price_observed_at_ms,
    NULL::numeric AS before_event_price_usd,
    NULL::numeric AS before_event_price_quote,
    NULL::text AS before_event_price_quote_symbol,
    NULL::text AS before_event_price_basis,
    false AS first_seen_global_24h
  FROM source_edges
  JOIN events ON events.event_id = source_edges.event_id
  JOIN token_intents ON token_intents.intent_id = source_edges.intent_id
  JOIN token_intent_resolutions
    ON token_intent_resolutions.resolution_id = source_edges.resolution_id
  LEFT JOIN registry_assets
    ON events.received_at_ms >= source_edges.score_since_ms
   AND source_edges.target_type = 'Asset'
   AND registry_assets.asset_id = source_edges.target_id
  LEFT JOIN asset_identity_current
    ON events.received_at_ms >= source_edges.score_since_ms
   AND source_edges.target_type = 'Asset'
   AND asset_identity_current.asset_id = source_edges.target_id
  LEFT JOIN cex_tokens
    ON events.received_at_ms >= source_edges.score_since_ms
   AND source_edges.target_type = 'CexToken'
   AND cex_tokens.cex_token_id = source_edges.target_id
  LEFT JOIN LATERAL (
    SELECT *
    FROM price_feeds
    WHERE source_edges.target_type = 'CexToken'
      AND price_feeds.subject_type = 'CexToken'
      AND price_feeds.subject_id = source_edges.target_id
      AND price_feeds.provider = 'binance'
      AND price_feeds.feed_type = 'cex_swap'
      AND price_feeds.quote_symbol = 'USDT'
      AND price_feeds.status = 'canonical'
    ORDER BY price_feeds.updated_at_ms DESC, price_feeds.native_market_id ASC
    LIMIT 1
  ) preferred_price_feed ON events.received_at_ms >= source_edges.score_since_ms
  LEFT JOIN price_feeds
    ON events.received_at_ms >= source_edges.score_since_ms
   AND price_feeds.pricefeed_id = CASE
      WHEN source_edges.target_type = 'CexToken' THEN preferred_price_feed.pricefeed_id
      ELSE source_edges.pricefeed_id
    END
  LEFT JOIN LATERAL (
    SELECT
      CASE
        WHEN source_edges.target_type = 'Asset'
          AND registry_assets.chain_id IS NOT NULL
          AND registry_assets.address IS NOT NULL
          THEN 'chain_token'
        WHEN source_edges.target_type = 'CexToken'
          AND price_feeds.provider IS NOT NULL
          AND price_feeds.native_market_id IS NOT NULL
          THEN 'cex_symbol'
        ELSE NULL
      END AS target_type,
      CASE
        WHEN source_edges.target_type = 'Asset'
          AND registry_assets.chain_id IS NOT NULL
          AND registry_assets.address IS NOT NULL
          THEN registry_assets.chain_id || ':' || registry_assets.address
        WHEN source_edges.target_type = 'CexToken'
          AND price_feeds.provider IS NOT NULL
          AND price_feeds.native_market_id IS NOT NULL
          THEN price_feeds.provider || ':' || price_feeds.native_market_id
        ELSE NULL
      END AS target_id
  ) market_target ON events.received_at_ms >= source_edges.score_since_ms
  LEFT JOIN LATERAL (
    SELECT
      enriched_events.tick_observed_at_ms,
      enriched_events.tick_id,
      enriched_events.capture_method,
      enriched_events.capture_reason,
      enriched_events.tick_lag_ms,
      enriched_events.created_at_ms
    FROM enriched_events
    WHERE events.received_at_ms >= source_edges.score_since_ms
      AND enriched_events.event_id = source_edges.event_id
      AND enriched_events.intent_id = source_edges.intent_id
      AND enriched_events.resolution_id = source_edges.resolution_id
    ORDER BY enriched_events.created_at_ms DESC
    LIMIT 1
  ) event_price_capture ON true
  LEFT JOIN market_ticks event_price_tick
    ON event_price_tick.observed_at_ms = event_price_capture.tick_observed_at_ms
   AND event_price_tick.tick_id = event_price_capture.tick_id
   AND event_price_tick.target_type = market_target.target_type
   AND event_price_tick.target_id = market_target.target_id
   AND event_price_tick.source_provider = CASE
      WHEN source_edges.target_type = 'CexToken' THEN 'binance_cex_rest'
      ELSE event_price_tick.source_provider
    END
  LEFT JOIN market_tick_current latest_price_tick
    ON latest_price_tick.target_type = market_target.target_type
   AND latest_price_tick.target_id = market_target.target_id
)
SELECT *
FROM hydrated
ORDER BY request_key ASC, source_rank ASC, received_at_ms ASC, event_id ASC
"""
