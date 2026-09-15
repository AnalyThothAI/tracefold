"""Read-only counts behind `tracefold news wallets` (#649 §7.2).

Five bounded aggregates over the tables the wallet flow already writes. They exist because "no alert"
has at least four different causes -- the roster cannot reach the threshold, the addresses have not
been monitored long enough, the facts were collected but never derived, or an intent exists and is
stuck behind the send queue -- and an empty event list says all four at once.

Two rules the numbers here keep:

* **A cumulative counter is never presented as a 24-hour rate.** Every window figure carries its own
  `from_ms`, and the tape state row's lifetime totals are reported as lifetime totals.
* **Different units are never added.** Fills, receipts, addresses, tokens, episodes and deliveries are
  counted separately and never summed into one "events" number.

Nothing here is registered in the query audit: these are operator-run diagnostics rather than route
statements, and each is bounded by an explicit time window or by the single roster/tape state row.
"""

from __future__ import annotations

from typing import Any, Final

WALLET_FLOW_COVERAGE_SQL: Final = """
    SELECT count(*) AS fills,
           count(DISTINCT tx_hash) AS receipts,
           count(DISTINCT wallet) AS wallets,
           count(DISTINCT token) AS tokens,
           count(*) FILTER (WHERE kind = 'buy') AS buys,
           count(*) FILTER (WHERE kind = 'sell') AS sells,
           count(*) FILTER (WHERE kind = 'transfer_out') AS transfers_out,
           count(*) FILTER (WHERE usd IS NOT NULL) AS priced,
           count(*) FILTER (WHERE usd IS NULL) AS unpriced,
           count(*) FILTER (WHERE derived_at_ms IS NULL) AS underived,
           min(event_at_ms) FILTER (WHERE derived_at_ms IS NULL) AS oldest_underived_at_ms
      FROM news_market_wallet_fills
     WHERE event_at_ms >= %s AND event_at_ms < %s
"""

WALLET_DERIVED_REASONS_SQL: Final = """
    SELECT COALESCE(derived_reason, '(not derived)') AS reason, count(*) AS fills
      FROM news_market_wallet_fills
     WHERE event_at_ms >= %s AND event_at_ms < %s
     GROUP BY 1 ORDER BY 2 DESC, 1
"""

WALLET_EPISODE_FUNNEL_SQL: Final = """
    SELECT count(*) AS episodes,
           count(*) FILTER (WHERE e.ended_at_ms IS NULL) AS active,
           count(*) FILTER (WHERE e.notification_eligible) AS eligible,
           count(d.delivery_key) AS intents,
           count(*) FILTER (WHERE d.state = 'sent') AS sent,
           count(*) FILTER (WHERE d.state = 'failed') AS failed,
           count(*) FILTER (WHERE d.state = 'unknown') AS unknown,
           count(*) FILTER (WHERE d.state = 'sending') AS sending,
           count(*) FILTER (WHERE d.state = 'unavailable') AS unavailable,
           count(*) FILTER (WHERE d.state = 'pending') AS pending
      FROM news_market_wallet_events e
      JOIN news_items i ON i.item_id = e.item_id
      LEFT JOIN news_market_deliveries d ON d.delivery_key = i.market_notify_delivery_key
     WHERE e.event_at_ms >= %s AND e.event_at_ms < %s
"""

WALLET_EPISODE_REASONS_SQL: Final = """
    SELECT COALESCE(NULLIF(t.pending_reason, ''), e.notification_reason, '(none)') AS reason,
           count(*) AS episodes
      FROM news_market_wallet_events e
      JOIN news_items i ON i.item_id = e.item_id
      LEFT JOIN news_market_deliveries d ON d.delivery_key = i.market_notify_delivery_key
      LEFT JOIN news_market_tracks t ON t.group_key = i.market_notify_group_key
     WHERE e.event_at_ms >= %s AND e.event_at_ms < %s AND d.state IS DISTINCT FROM 'sent'
     GROUP BY 1 ORDER BY 2 DESC, 1
"""

# The shared send queue, in the order `market_due_delivery` reads it. Every family, because the
# question this answers is whether one card is holding up the others (#649 §7.2).
WALLET_SEND_QUEUE_SQL: Final = """
    SELECT delivery_key, market_kind, state, attempts, next_attempt_at_ms, error, created_at_ms
      FROM news_market_deliveries
     WHERE state = ANY (ARRAY['pending', 'unavailable'])
     ORDER BY next_attempt_at_ms, created_at_ms, delivery_key
     LIMIT %s
"""


class WalletDiagnosticsStorage:
    conn: Any

    def wallet_flow_coverage(self, *, from_ms: int, to_ms: int) -> dict[str, Any]:
        """One window of collected fills, counted per unit rather than added together."""

        row = self.conn.execute(WALLET_FLOW_COVERAGE_SQL, (int(from_ms), int(to_ms))).fetchone()
        return dict(row or {})

    def wallet_derived_reasons(self, *, from_ms: int, to_ms: int) -> list[dict[str, Any]]:
        return [dict(row) for row in self.conn.execute(WALLET_DERIVED_REASONS_SQL, (int(from_ms), int(to_ms)))]

    def wallet_episode_funnel(self, *, from_ms: int, to_ms: int) -> dict[str, Any]:
        row = self.conn.execute(WALLET_EPISODE_FUNNEL_SQL, (int(from_ms), int(to_ms))).fetchone()
        return dict(row or {})

    def wallet_episode_reasons(self, *, from_ms: int, to_ms: int) -> list[dict[str, Any]]:
        return [dict(row) for row in self.conn.execute(WALLET_EPISODE_REASONS_SQL, (int(from_ms), int(to_ms)))]

    def wallet_send_queue(self, *, limit: int = 10) -> list[dict[str, Any]]:
        return [dict(row) for row in self.conn.execute(WALLET_SEND_QUEUE_SQL, (max(1, int(limit)),))]


__all__ = [
    "WALLET_DERIVED_REASONS_SQL",
    "WALLET_EPISODE_FUNNEL_SQL",
    "WALLET_EPISODE_REASONS_SQL",
    "WALLET_FLOW_COVERAGE_SQL",
    "WALLET_SEND_QUEUE_SQL",
    "WalletDiagnosticsStorage",
]
