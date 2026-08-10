from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from tracefold.market.identity.resolver_policy import TOKEN_RESOLVER_POLICY_VERSION
from tracefold.market.radar.stocks_projection import (
    compute_stocks_radar_target_feature,
    rank_stocks_radar,
)
from tracefold.market.windows import PRODUCT_WINDOW_MS
from tracefold.platform.postgres.postgres_client import require_transaction

STOCKS_RADAR_INPUT_ROW_CAP = 25_000
STOCKS_RADAR_INPUT_BYTE_CAP = 16 * 1024 * 1024
STOCKS_RADAR_REDUCER_BUDGET_SECONDS = 8.0


class StocksRadarInputOverflow(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ReducedStocksRadar:
    input_rows: int
    projections: dict[str, dict[str, Any]]
    features: dict[str, tuple[dict[str, Any], ...]]


def reduce_stocks_radar(
    rows: Sequence[Mapping[str, Any]],
    *,
    now_ms: int,
) -> ReducedStocksRadar:
    if len(rows) > STOCKS_RADAR_INPUT_ROW_CAP:
        raise StocksRadarInputOverflow("stocks_radar_input_row_overflow")
    if _serialized_size(rows) > STOCKS_RADAR_INPUT_BYTE_CAP:
        raise StocksRadarInputOverflow("stocks_radar_input_byte_overflow")
    material = _deduplicated_rows(rows)
    projections: dict[str, dict[str, Any]] = {}
    features_by_window: dict[str, tuple[dict[str, Any], ...]] = {}
    for window, duration_ms in PRODUCT_WINDOW_MS.items():
        since_ms = max(0, int(now_ms) - duration_ms)
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in material:
            received_at_ms = int(row.get("received_at_ms") or 0)
            if since_ms <= received_at_ms <= int(now_ms):
                grouped[str(row["target_id"])].append(row)
        features: list[dict[str, Any]] = []
        for target_id, target_rows in sorted(grouped.items()):
            projected = compute_stocks_radar_target_feature(
                {
                    "target_id": target_id,
                    "window": window,
                    "now_ms": int(now_ms),
                    "rows": target_rows,
                }
            )
            feature = projected.get("feature")
            if isinstance(feature, dict):
                features.append(feature)
        features_by_window[window] = tuple(features)
        projections[window] = rank_stocks_radar(
            {
                "window": window,
                "now_ms": int(now_ms),
                "current_features": features,
            }
        )
    return ReducedStocksRadar(
        input_rows=len(rows),
        projections=projections,
        features=features_by_window,
    )


class StocksRadarCurrentRepository:
    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def load_material_inputs(self, *, now_ms: int) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.conn.execute(
                _STOCKS_RADAR_INPUT_SQL,
                (
                    max(0, int(now_ms) - PRODUCT_WINDOW_MS["24h"]),
                    int(now_ms),
                    TOKEN_RESOLVER_POLICY_VERSION,
                    STOCKS_RADAR_INPUT_ROW_CAP + 1,
                ),
            ).fetchall()
        ]

    def publish(self, reduced: ReducedStocksRadar, *, now_ms: int) -> int:
        require_transaction(self.conn, operation="publish_stocks_radar_current")
        writes = 0
        for window in PRODUCT_WINDOW_MS:
            features = list(reduced.features[window])
            projection = reduced.projections[window]
            self.conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext('stocks_radar_current'), hashtext(%s))",
                (window,),
            )
            writes += self._sync_features(window=window, features=features)
            state = self.conn.execute(
                """
                SELECT state_fingerprint
                  FROM stocks_radar_publication_state
                 WHERE window_key = %s
                 FOR UPDATE
                """,
                (window,),
            ).fetchone()
            if state is not None and str(state["state_fingerprint"]) == str(projection["state_fingerprint"]):
                continue
            writes += int(
                self.conn.execute(
                    "DELETE FROM stocks_radar_current_rows WHERE window_key = %s",
                    (window,),
                ).rowcount
                or 0
            )
            for row in projection["rows"]:
                writes += int(
                    self.conn.execute(
                        """
                        INSERT INTO stocks_radar_current_rows (
                          window_key, target_id, rank, symbol,
                          security_name, exchange, instrument_type,
                          mentions, unique_authors, latest_seen_ms,
                          latest_event_id, latest_author_handle,
                          latest_text, source_event_ids,
                          state_fingerprint, computed_at_ms
                        )
                        VALUES (
                          %(window_key)s, %(target_id)s, %(rank)s,
                          %(symbol)s, %(security_name)s, %(exchange)s,
                          %(instrument_type)s, %(mentions)s,
                          %(unique_authors)s, %(latest_seen_ms)s,
                          %(latest_event_id)s, %(latest_author_handle)s,
                          %(latest_text)s, %(source_event_ids)s,
                          %(state_fingerprint)s, %(computed_at_ms)s
                        )
                        """,
                        row,
                    ).rowcount
                    or 0
                )
            writes += int(
                self.conn.execute(
                    """
                    INSERT INTO stocks_radar_publication_state(
                      window_key, state_fingerprint,
                      source_frontier_ms, published_at_ms
                    )
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (window_key) DO UPDATE SET
                      state_fingerprint = EXCLUDED.state_fingerprint,
                      source_frontier_ms = EXCLUDED.source_frontier_ms,
                      published_at_ms = EXCLUDED.published_at_ms
                    """,
                    (
                        window,
                        projection["state_fingerprint"],
                        int(projection["source_frontier_ms"]),
                        int(now_ms),
                    ),
                ).rowcount
                or 0
            )
        return writes

    def _sync_features(self, *, window: str, features: list[dict[str, Any]]) -> int:
        writes = 0
        target_ids = [str(feature["target_id"]) for feature in features]
        if target_ids:
            writes += int(
                self.conn.execute(
                    """
                    DELETE FROM stock_attention_target_features
                     WHERE window_key = %s
                       AND NOT (target_id = ANY(%s::text[]))
                    """,
                    (window, target_ids),
                ).rowcount
                or 0
            )
        else:
            writes += int(
                self.conn.execute(
                    "DELETE FROM stock_attention_target_features WHERE window_key = %s",
                    (window,),
                ).rowcount
                or 0
            )
        for feature in features:
            writes += int(
                self.conn.execute(
                    """
                    INSERT INTO stock_attention_target_features (
                      window_key, target_id, symbol, security_name,
                      exchange, instrument_type, mentions, unique_authors,
                      latest_seen_ms, latest_event_id,
                      latest_author_handle, latest_text, source_event_ids,
                      state_fingerprint, computed_at_ms
                    )
                    VALUES (
                      %(window_key)s, %(target_id)s, %(symbol)s,
                      %(security_name)s, %(exchange)s,
                      %(instrument_type)s, %(mentions)s,
                      %(unique_authors)s, %(latest_seen_ms)s,
                      %(latest_event_id)s, %(latest_author_handle)s,
                      %(latest_text)s, %(source_event_ids)s,
                      %(state_fingerprint)s, %(computed_at_ms)s
                    )
                    ON CONFLICT (window_key, target_id) DO UPDATE SET
                      symbol = EXCLUDED.symbol,
                      security_name = EXCLUDED.security_name,
                      exchange = EXCLUDED.exchange,
                      instrument_type = EXCLUDED.instrument_type,
                      mentions = EXCLUDED.mentions,
                      unique_authors = EXCLUDED.unique_authors,
                      latest_seen_ms = EXCLUDED.latest_seen_ms,
                      latest_event_id = EXCLUDED.latest_event_id,
                      latest_author_handle = EXCLUDED.latest_author_handle,
                      latest_text = EXCLUDED.latest_text,
                      source_event_ids = EXCLUDED.source_event_ids,
                      state_fingerprint = EXCLUDED.state_fingerprint,
                      computed_at_ms = EXCLUDED.computed_at_ms
                    WHERE stock_attention_target_features.state_fingerprint
                          IS DISTINCT FROM EXCLUDED.state_fingerprint
                    """,
                    feature,
                ).rowcount
                or 0
            )
        return writes


def _deduplicated_rows(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for raw in rows:
        target_id = str(raw.get("target_id") or "").strip()
        event_id = str(raw.get("event_id") or "").strip()
        if not target_id or not event_id:
            continue
        row = dict(raw)
        key = (target_id, event_id)
        previous = by_key.get(key)
        if previous is None or _stock_event_key(row) < _stock_event_key(previous):
            by_key[key] = row
    return sorted(by_key.values(), key=_stock_event_key)


def _stock_event_key(row: Mapping[str, Any]) -> tuple[int, str, str]:
    return (
        int(row.get("received_at_ms") or 0),
        str(row.get("event_id") or ""),
        str(row.get("target_id") or ""),
    )


def _serialized_size(rows: Sequence[Mapping[str, Any]]) -> int:
    return len(
        json.dumps(
            rows,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        ).encode("utf-8")
    )


_STOCKS_RADAR_INPUT_SQL = """
SELECT DISTINCT ON (resolution.target_id, event.event_id)
  resolution.target_id,
  instrument.symbol,
  instrument.security_name,
  instrument.exchange,
  instrument.instrument_type,
  event.event_id,
  event.received_at_ms,
  event.author_handle,
  COALESCE(event.text_clean, event.search_text, event.text, '') AS text
FROM events event
JOIN token_intents intent
  ON intent.event_id = event.event_id
JOIN token_intent_resolutions resolution
  ON resolution.intent_id = intent.intent_id
 AND resolution.event_id = event.event_id
JOIN us_equity_symbols instrument
  ON instrument.market_instrument_id = resolution.target_id
 AND instrument.status = 'active'
WHERE event.received_at_ms >= %s
  AND event.received_at_ms <= %s
  AND resolution.is_current = true
  AND resolution.target_type = 'MarketInstrument'
  AND resolution.resolution_status = 'NON_CRYPTO'
  AND resolution.resolver_policy_version = %s
  AND resolution.reason_codes_json @> '["CONFIRMED_US_EQUITY"]'::jsonb
ORDER BY
  resolution.target_id,
  event.event_id,
  resolution.decision_time_ms DESC,
  resolution.resolution_id ASC,
  intent.intent_id ASC
LIMIT %s
"""


__all__ = [
    "STOCKS_RADAR_INPUT_BYTE_CAP",
    "STOCKS_RADAR_INPUT_ROW_CAP",
    "STOCKS_RADAR_REDUCER_BUDGET_SECONDS",
    "ReducedStocksRadar",
    "StocksRadarCurrentRepository",
    "StocksRadarInputOverflow",
    "reduce_stocks_radar",
]
