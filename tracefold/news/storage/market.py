"""The market read model: Items and their typed facts, read directly.

Nothing here goes through a verdict, a reader-history snapshot, an Event leader or a model. A market
observation exists because the provider reported it and this process stored it, and that is the whole
question this module answers (#553).

The list collapses *consecutive observations of the same group*, and the group is per kind: an OI
group is one provider, venue, native instrument and measurement definition; a liquidation group adds
the liquidated side instead of the definition; a smart-money group is one account acting one way on
one instrument. A uniform "latest row per symbol" would let one account's Close bury another
account's Open, and let a Binance liquidation bury an OKX one. An observation with no trustworthy
group fields is its own group -- unknown does not merge with unknown.

`notification_status` is reported beside `parse_status` and never folded into it. A raw card that was
sent and a parsed card that was not are both ordinary states, and one combined "outcome" column would
have to lie about one of them.

"Consecutive" is consecutive *in what the reader asked for*. Narrowing to one kind merges a run that
an observation of another kind sat between, which is the honest answer to the narrowed question: the
row that would have broken the run is not on the page, so reporting two groups there would show a
break the reader cannot see. The unfiltered list keeps every break.
"""

from __future__ import annotations

from typing import Any, Final, TypedDict

from ..market_contracts import (
    MARKET_TIMELINE_MAX,
    MARKET_WINDOW_ROW_CAP,
    NOTIFICATION_REASON_NOT_CONNECTED,
    NOTIFICATION_STATUS_NOT_CONNECTED,
)
from ..source_contracts import MARKET_KINDS


class MarketObservationRow(TypedDict):
    """One provider record, its typed fact if it has one, and the group it belongs to."""

    item_id: str
    market_kind: str
    source_strategy_id: str | None
    parse_status: str
    parse_error: str | None
    ingest_mode: str
    historical: bool
    group_key: str
    title: str
    event_at_ms: int
    received_at_ms: int
    available_at_ms: int | None
    provider: str | None
    source_venue: str | None
    raw_instrument: str | None
    symbol: str | None
    measurement_definition: str | None
    direction: str | None
    oi_change_bps: int | None
    oi_value_usd: int | None
    whale_long_profit_bps: int | None
    whale_oi_ratio_bps: int | None
    liquidated_position_side: str | None
    forced_order_side: str | None
    notional_usd: str | None
    price: str | None
    trader_label: str | None
    account_address: str | None
    action: str | None
    position_side: str | None
    pnl_usd: str | None


class MarketGroupRow(TypedDict):
    """One run of consecutive observations of the same group, newest first."""

    group_key: str
    market_kind: str
    observation_count: int
    first_event_at_ms: int
    last_event_at_ms: int
    latest: MarketObservationRow
    notification_status: str
    notification_reason: str


class MarketSourceSummaryRow(TypedDict):
    """What one market kind did in the requested window."""

    market_kind: str
    received: int
    parsed: int
    raw: int
    groups: int
    last_received_at_ms: int | None


# One observation, whatever kind it is. The three fact tables stay separate tables -- a shared
# supertable would need a column for every kind's semantics and a NULL for every other kind's -- and
# they are unioned into one shape only here, at the point a reader actually needs one list.
_OBSERVATIONS_SQL = """
    SELECT i.item_id,
           i.market_kind,
           i.market_source_strategy_id AS source_strategy_id,
           i.market_parse_status AS parse_status,
           i.market_parse_error AS parse_error,
           i.first_ingest_mode AS ingest_mode,
           COALESCE(o.historical, false) AS historical,
           i.title,
           i.published_at_ms AS event_at_ms,
           i.observed_at_ms AS received_at_ms,
           COALESCE(o.available_at_ms, l.available_at_ms, w.available_at_ms) AS available_at_ms,
           COALESCE(o.provider, l.provider, w.provider) AS provider,
           COALESCE(o.source_venue, l.source_venue, w.source_venue) AS source_venue,
           COALESCE(o.raw_instrument, l.raw_instrument, w.raw_instrument) AS raw_instrument,
           COALESCE(o.symbol, l.symbol, w.symbol) AS symbol,
           o.measurement_definition,
           o.direction,
           o.oi_change_bps,
           o.oi_value_usd,
           o.whale_long_profit_bps,
           o.whale_oi_ratio_bps,
           l.liquidated_position_side,
           l.forced_order_side,
           COALESCE(l.notional_usd, w.reported_notional_usd)::text AS notional_usd,
           COALESCE(l.price, w.price)::text AS price,
           w.trader_label,
           w.account_address,
           w.action,
           w.position_side,
           w.pnl_usd::text AS pnl_usd,
           CASE
             WHEN o.source_item_id IS NOT NULL THEN
               'oi|' || o.provider || '|' || COALESCE(o.source_venue, '') || '|'
                     || o.raw_instrument || '|' || o.measurement_definition
             WHEN l.item_id IS NOT NULL THEN
               'liquidation|' || l.provider || '|' || COALESCE(l.source_venue, '') || '|'
                     || l.raw_instrument || '|' || l.liquidated_position_side
             WHEN w.item_id IS NOT NULL THEN
               'smart_money|' || w.provider || '|' || w.source_strategy_id || '|' || w.trader_label
                     || '|' || COALESCE(w.account_address, '') || '|' || COALESCE(w.source_venue, '')
                     || '|' || w.raw_instrument || '|' || w.action || '|' || w.position_side
             ELSE 'raw|' || i.market_kind || '|' || i.item_id
           END AS group_key
      FROM news_items i
      LEFT JOIN news_oi_signals o ON o.source_item_id = i.item_id
      LEFT JOIN news_market_liquidations l ON l.item_id = i.item_id
      LEFT JOIN news_market_smart_money w ON w.item_id = i.item_id
"""

_OBSERVATION_KEYS: Final[tuple[str, ...]] = (
    "item_id",
    "market_kind",
    "source_strategy_id",
    "parse_status",
    "parse_error",
    "ingest_mode",
    "historical",
    "group_key",
    "title",
    "event_at_ms",
    "received_at_ms",
    "available_at_ms",
    "provider",
    "source_venue",
    "raw_instrument",
    "symbol",
    "measurement_definition",
    "direction",
    "oi_change_bps",
    "oi_value_usd",
    "whale_long_profit_bps",
    "whale_oi_ratio_bps",
    "liquidated_position_side",
    "forced_order_side",
    "notional_usd",
    "price",
    "trader_label",
    "account_address",
    "action",
    "position_side",
    "pnl_usd",
)

MARKET_GROUPS_SQL = f"""
    WITH observations AS MATERIALIZED (
      SELECT * FROM ({_OBSERVATIONS_SQL}
         WHERE i.market_kind IS NOT NULL
           AND i.market_kind = ANY(%s)
           AND i.observed_at_ms >= %s
           AND i.observed_at_ms < %s
         ORDER BY i.observed_at_ms DESC, i.item_id DESC
         LIMIT {MARKET_WINDOW_ROW_CAP}) AS windowed
    ), islands AS (
      SELECT observations.*,
             row_number() OVER (ORDER BY received_at_ms DESC, item_id DESC)
               - row_number() OVER (PARTITION BY group_key ORDER BY received_at_ms DESC, item_id DESC) AS island
        FROM observations
    ), collapsed AS (
      SELECT group_key, island, count(*) AS observation_count,
             min(event_at_ms) AS first_event_at_ms,
             max(event_at_ms) AS last_event_at_ms,
             (array_agg(item_id ORDER BY received_at_ms DESC, item_id DESC))[1] AS latest_item_id,
             max(received_at_ms) AS sort_received_at_ms
        FROM islands
       GROUP BY group_key, island
    )
    SELECT c.observation_count, c.first_event_at_ms, c.last_event_at_ms, i.*
      FROM collapsed c
      JOIN islands i ON i.item_id = c.latest_item_id
     WHERE (c.sort_received_at_ms, c.latest_item_id) < (%s, %s)
     ORDER BY c.sort_received_at_ms DESC, c.latest_item_id DESC
     LIMIT %s
"""  # noqa: S608 -- the only interpolation is this module's own row cap and column list

# The second `news_items` reference is a primary-key lookup for the three columns only the detail page
# needs. Widening the shared observation projection with them would put a provider payload into every
# row of every list page to serve one row of one detail page.
MARKET_ITEM_SQL = f"""
    SELECT windowed.*, i2.provider_params, i2.description, i2.raw_first_line
      FROM ({_OBSERVATIONS_SQL} WHERE i.item_id = %s AND i.market_kind IS NOT NULL) AS windowed
      JOIN news_items i2 ON i2.item_id = windowed.item_id
"""  # noqa: S608 -- interpolates only this module's own observation projection

MARKET_TIMELINE_SQL = f"""
    SELECT * FROM ({_OBSERVATIONS_SQL} WHERE i.market_kind IS NOT NULL) AS windowed
     WHERE windowed.group_key = %s
     ORDER BY windowed.received_at_ms DESC, windowed.item_id DESC
     LIMIT {MARKET_TIMELINE_MAX}
"""  # noqa: S608 -- interpolates only this module's own timeline cap

MARKET_SOURCES_SQL = f"""
    WITH observations AS MATERIALIZED (
      SELECT * FROM ({_OBSERVATIONS_SQL}
         WHERE i.market_kind IS NOT NULL
           AND i.observed_at_ms >= %s
           AND i.observed_at_ms < %s
         ORDER BY i.observed_at_ms DESC, i.item_id DESC
         LIMIT {MARKET_WINDOW_ROW_CAP}) AS windowed
    )
    SELECT market_kind,
           count(*) AS received,
           count(*) FILTER (WHERE parse_status = 'parsed') AS parsed,
           count(*) FILTER (WHERE parse_status = 'raw') AS raw,
           count(DISTINCT group_key) AS groups,
           max(received_at_ms) AS last_received_at_ms
      FROM observations
     GROUP BY market_kind
"""  # noqa: S608 -- interpolates only this module's own row cap


class MarketStorage:
    conn: Any

    def market_groups(
        self,
        *,
        kinds: tuple[str, ...],
        from_ms: int,
        to_ms: int,
        cursor_received_at_ms: int,
        cursor_item_id: str,
        limit: int,
    ) -> list[MarketGroupRow]:
        """One page of collapsed groups, newest observation first."""

        rows = self.conn.execute(
            MARKET_GROUPS_SQL,
            (
                list(kinds or MARKET_KINDS),
                int(from_ms),
                int(to_ms),
                int(cursor_received_at_ms),
                cursor_item_id,
                int(limit),
            ),
        ).fetchall()
        return [
            MarketGroupRow(
                group_key=str(row["group_key"]),
                market_kind=str(row["market_kind"]),
                observation_count=int(row["observation_count"]),
                first_event_at_ms=int(row["first_event_at_ms"]),
                last_event_at_ms=int(row["last_event_at_ms"]),
                latest=_observation(row),
                notification_status=NOTIFICATION_STATUS_NOT_CONNECTED,
                notification_reason=NOTIFICATION_REASON_NOT_CONNECTED,
            )
            for row in rows
        ]

    def market_item(self, *, item_id: str) -> dict[str, Any] | None:
        """One market Item with its stored provider payload. Not bound by the list's window."""

        row = self.conn.execute(MARKET_ITEM_SQL, (item_id,)).fetchone()
        if row is None:
            return None
        detail: dict[str, Any] = dict(_observation(row))
        detail["provider_params"] = dict(row["provider_params"] or {})
        detail["description"] = str(row["description"] or "")
        detail["raw_first_line"] = str(row["raw_first_line"] or "")
        detail["notification_status"] = NOTIFICATION_STATUS_NOT_CONNECTED
        detail["notification_reason"] = NOTIFICATION_REASON_NOT_CONNECTED
        return detail

    def market_group_timeline(self, *, group_key: str) -> list[MarketObservationRow]:
        """Every retained observation of one group, newest first."""

        rows = self.conn.execute(MARKET_TIMELINE_SQL, (group_key,)).fetchall()
        return [_observation(row) for row in rows]

    def market_sources(self, *, from_ms: int, to_ms: int) -> list[MarketSourceSummaryRow]:
        """Per-kind intake for the window: what arrived, what parsed, how much it collapses to."""

        rows = self.conn.execute(MARKET_SOURCES_SQL, (int(from_ms), int(to_ms))).fetchall()
        by_kind = {str(row["market_kind"]): row for row in rows}
        return [
            MarketSourceSummaryRow(
                market_kind=kind,
                received=int(by_kind[kind]["received"]) if kind in by_kind else 0,
                parsed=int(by_kind[kind]["parsed"]) if kind in by_kind else 0,
                raw=int(by_kind[kind]["raw"]) if kind in by_kind else 0,
                groups=int(by_kind[kind]["groups"]) if kind in by_kind else 0,
                last_received_at_ms=(
                    int(by_kind[kind]["last_received_at_ms"])
                    if kind in by_kind and by_kind[kind]["last_received_at_ms"] is not None
                    else None
                ),
            )
            for kind in MARKET_KINDS
        ]


def _observation(row: Any) -> MarketObservationRow:
    """Name every projected column once. No coercion beyond the two integer identities."""

    values: dict[str, Any] = {key: row[key] for key in _OBSERVATION_KEYS}
    values["historical"] = bool(values["historical"])
    values["event_at_ms"] = int(values["event_at_ms"])
    values["received_at_ms"] = int(values["received_at_ms"])
    observation: MarketObservationRow = values  # type: ignore[assignment]
    return observation


__all__ = [
    "MARKET_GROUPS_SQL",
    "MARKET_ITEM_SQL",
    "MARKET_SOURCES_SQL",
    "MARKET_TIMELINE_SQL",
    "MarketGroupRow",
    "MarketObservationRow",
    "MarketSourceSummaryRow",
    "MarketStorage",
]
