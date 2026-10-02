"""Market fact writes, delivered-card settlement, and OI-facing pushed News."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# S608 exemptions below interpolate only closed, module-owned predicates; all values stay bound.
from ..market_contracts import MARKET_NEWS_PUSHED_MAX, MARKET_NEWS_WINDOW_MS
from ..models import TelegramDeliveryReceipt
from .feed_sql import EDITORIAL_EVENT_SQL
from .sql_values import _dumps

_PUSHED_NEWS_PROJECTION = """
    SELECT d.event_id, d.settled_at_ms AS at_ms,
           COALESCE(d.history_context ->> 'storyline_key', '') AS storyline_key,
           COALESCE(d.history_context ->> 'comparison_title', '') AS comparison_title,
           COALESCE(d.history_context ->> 'comparison_fingerprint', '') AS comparison_fingerprint,
           COALESCE(d.history_context ->> 'dedupe_family', 'general') AS dedupe_family,
           COALESCE(d.history_context ->> 'direction', 'unclear') AS direction,
           COALESCE(d.history_context ->> 'headline_zh', '') AS headline_zh,
           COALESCE(d.history_context ->> 'why_zh', '') AS why_zh,
           COALESCE(d.history_context -> 'grounded_assets', '[]'::jsonb) AS grounded_assets,
           COALESCE(d.history_context -> 'assets', '[]'::jsonb) AS assets,
           COALESCE(d.history_context -> 'canonical_assets', '[]'::jsonb) AS canonical_assets
      FROM news_events e
      JOIN news_notifications d ON d.event_id = e.event_id AND d.kind = 'update' AND d.state = 'sent'
"""


# #582 §3.3. The News an OI card's instrument already has, in the two numbers that card prints. Here
# rather than beside the market statements because this is the *delivered-card* ledger -- the same
# rows and the same `update` / `sent` / not-deleted predicate.
#
# The symbol is resolved through `news_symbol_aliases`: the alias's base, plus every alias of
# that base. An OI frame naming `9988`
# and a story tagged `BABA` are the same instrument to a reader, and a card that said `共 0` beside a
# story it had just pushed about the same company would be wrong in the one way this line exists to
# fix.
_EQUIVALENT_SYMBOLS_CTE = """
    WITH current_bases AS (
      SELECT COALESCE(alias.base_symbol, requested.symbol) AS base
        FROM (SELECT %s::text AS symbol) requested
        LEFT JOIN news_symbol_aliases alias ON alias.alias = requested.symbol
    ), equivalent_symbols AS (
      SELECT base AS symbol FROM current_bases
      UNION
      SELECT a.alias FROM news_symbol_aliases a JOIN current_bases b ON b.base = a.base_symbol
    )
"""
# The titles, newest first, bounded by the window and by `LIMIT`. Two window predicates because the
# card prints two numbers about one set: `settled_at_ms` is what "已推" means -- when the reader was
# actually interrupted, answered by `ix_news_deliveries_sent` -- and `opened_at_ms` is the same bound
# the total below counts with, so what is quoted is always a subset of what is counted. Without the
# second one a card pushed 10 h ago for an Event opened 50 h ago read `已推 1 · 共 0`, and a `共 0`
# card prints nothing at all: the headline was silently dropped rather than shown.
MARKET_NEWS_PUSHED_SQL = f"""{_EQUIVALENT_SYMBOLS_CTE}{_PUSHED_NEWS_PROJECTION}
     WHERE {EDITORIAL_EVENT_SQL}
       AND e.opened_at_ms >= %s
       AND d.settled_at_ms >= %s
       AND EXISTS (
         SELECT 1 FROM news_event_assets candidate_asset
          WHERE candidate_asset.event_id = e.event_id
            AND candidate_asset.symbol IN (SELECT symbol FROM equivalent_symbols)
       )
     ORDER BY d.settled_at_ms DESC, d.event_id
     LIMIT %s
"""  # noqa: S608 - the only interpolation is this package's own Event-kind predicate
# The denominator, and a different question: how many editorial Events named this instrument at all,
# pushed or not. Read from `news_event_assets` rather than from `news_events` because that is where
# the bound is indexed -- `ix_news_event_assets_symbol (symbol, opened_at_ms DESC)` -- and every asset
# row carries its Event's own `opened_at_ms`, so the window is the Event's. `count(DISTINCT event_id)`
# because one Event may carry the same instrument under two of its aliases.
MARKET_NEWS_TOTAL_SQL = f"""{_EQUIVALENT_SYMBOLS_CTE}
    SELECT count(DISTINCT ea.event_id) AS total
      FROM news_event_assets ea
      JOIN news_events e ON e.event_id = ea.event_id
     WHERE ea.symbol IN (SELECT symbol FROM equivalent_symbols)
       AND ea.opened_at_ms >= %s
       AND {EDITORIAL_EVENT_SQL}
"""  # noqa: S608 - the only interpolation is this package's own Event-kind predicate


class DecisionStorage:
    conn: Any

    def pushed_news_for_symbol(self, symbol: str, *, now_ms: int) -> dict[str, Any]:
        """The News an OI card's instrument already has: the pushed titles, and how many Events (#582 §3.3).

        Two statements because they answer two questions with two windows. `pushed` is what the reader
        was actually interrupted with, bounded by when the card settled and by `MARKET_NEWS_PUSHED_MAX`;
        `total` is how many editorial Events named this instrument, bounded by when they opened. Both
        carry the Event window, so `pushed` is always a subset of `total` and the card's two numbers
        describe one set; only the pushed half additionally asks when the reader was interrupted.

        A row whose card and verdict both left the title empty is dropped rather than returned: the
        card prints one line per entry and counts what it printed, so an untitled row would be a line
        that says only a time, or a count that does not match the lines under it.

        Display only, and it may not raise into a send: an empty symbol is answered here rather than
        with a read, and everything else the loop degrades to no line.
        """

        requested = str(symbol or "").strip()
        if not requested:
            return {"pushed": [], "total": 0}
        cutoff_ms = int(now_ms) - MARKET_NEWS_WINDOW_MS
        pushed = self.conn.execute(
            MARKET_NEWS_PUSHED_SQL, (requested, cutoff_ms, cutoff_ms, MARKET_NEWS_PUSHED_MAX)
        ).fetchall()
        counted = self.conn.execute(MARKET_NEWS_TOTAL_SQL, (requested, cutoff_ms)).fetchone()
        # One line per Event: an Event notified again by an update intent is still one of `total`.
        newest: dict[str, Any] = {}
        for row in pushed:
            newest.setdefault(str(row["event_id"]), row)
        return {
            "pushed": [
                {
                    "event_id": event_id,
                    "headline_zh": headline,
                    "at_ms": int(row["at_ms"] or 0),
                }
                for event_id, row in newest.items()
                if (headline := str(row["headline_zh"] or "").strip())
            ],
            "total": int(counted["total"] or 0) if counted is not None else 0,
        }

    def begin_delivery_edit(
        self,
        *,
        intent_id: str,
        card: Mapping[str, Any],
        receipt: Mapping[str, Any],
        now_ms: int,
    ) -> bool:
        """Persist the desired replacement before mutating one provider message.

        Keyed by the delivery intent. The frozen payload is never touched by an edit.
        """

        parsed = _telegram_receipt(receipt)
        if parsed is None:
            return False
        cursor = self.conn.execute(
            """
            UPDATE news_notifications
               SET edit_state = 'editing', pending_card = %s::jsonb,
                   edit_error_code = NULL, edit_attempted_at_ms = %s, edit_settled_at_ms = NULL
             WHERE intent_id = %s AND state = 'sent'
               AND receipt ->> 'provider' = %s
               AND receipt ->> 'message_id' = %s
               AND receipt ->> 'pushed_at_ms' = %s
               AND receipt ->> 'target_sha256' = %s
               AND (edit_state IS NULL OR edit_state = 'edited')
            """,
            (
                _dumps(dict(card)),
                int(now_ms),
                intent_id,
                parsed.provider,
                str(parsed.message_id),
                str(parsed.pushed_at_ms),
                parsed.target_sha256,
            ),
        )
        return bool(cursor.rowcount)

    def settle_delivery_edit(
        self,
        *,
        intent_id: str,
        receipt: Mapping[str, Any],
        now_ms: int,
    ) -> bool:
        """CAS a confirmed provider edit over its already-durable desired card.

        The provider's edited receipt is merged over the stored one, so the fields an update intent's
        settlement recorded beside it (channel, payload digest, message id) survive the edit. The
        frozen card retains the headline, claim refs, body and digest the reader was sent.
        """

        parsed = _telegram_receipt(receipt, require_edited=True)
        if parsed is None:
            return False
        cursor = self.conn.execute(
            """
            UPDATE news_notifications
               SET pending_card = NULL, receipt = receipt || %s::jsonb,
                   edit_state = 'edited', edit_error_code = NULL, edit_settled_at_ms = %s
             WHERE intent_id = %s AND state = 'sent' AND edit_state = 'editing'
               AND receipt ->> 'provider' = %s
               AND receipt ->> 'message_id' = %s
               AND receipt ->> 'pushed_at_ms' = %s
               AND receipt ->> 'target_sha256' = %s
            """,
            (
                _dumps(parsed.canonical()),
                int(now_ms),
                intent_id,
                parsed.provider,
                str(parsed.message_id),
                str(parsed.pushed_at_ms),
                parsed.target_sha256,
            ),
        )
        return bool(cursor.rowcount)

    def mark_delivery_edit_ambiguous(
        self,
        *,
        intent_id: str,
        receipt: Mapping[str, Any],
        error_code: str,
        now_ms: int,
    ) -> bool:
        """Record that an attempted provider mutation cannot be proved either way."""

        parsed = _telegram_receipt(receipt)
        normalized_error = str(error_code or "")
        if parsed is None or not normalized_error or len(normalized_error) > 160:
            return False
        cursor = self.conn.execute(
            """
            UPDATE news_notifications
               SET edit_state = 'ambiguous', edit_error_code = %s, edit_settled_at_ms = %s
             WHERE intent_id = %s AND state = 'sent' AND edit_state = 'editing'
               AND receipt ->> 'provider' = %s
               AND receipt ->> 'message_id' = %s
               AND receipt ->> 'pushed_at_ms' = %s
               AND receipt ->> 'target_sha256' = %s
            """,
            (
                normalized_error,
                int(now_ms),
                intent_id,
                parsed.provider,
                str(parsed.message_id),
                str(parsed.pushed_at_ms),
                parsed.target_sha256,
            ),
        )
        return bool(cursor.rowcount)

    def terminalize_interrupted_delivery_edits(self, *, now_ms: int) -> int:
        cursor = self.conn.execute(
            """
            UPDATE news_notifications
               SET edit_state = 'ambiguous', edit_error_code = 'edit_ambiguous_after_crash',
                   edit_settled_at_ms = %s
             WHERE edit_state = 'editing'
            """,
            (int(now_ms),),
        )
        return int(cursor.rowcount or 0)

    def terminalize_stale_delivery_edits(self, *, now_ms: int) -> int:
        cursor = self.conn.execute(
            """
            UPDATE news_notifications
               SET edit_state = 'ambiguous', edit_error_code = 'edit_settlement_unavailable',
                   edit_settled_at_ms = %s
             WHERE edit_state = 'editing' AND edit_attempted_at_ms < %s
            """,
            (int(now_ms), int(now_ms) - 60_000),
        )
        return int(cursor.rowcount or 0)


def _telegram_receipt(
    receipt: Mapping[str, Any],
    *,
    require_edited: bool = False,
) -> TelegramDeliveryReceipt | None:
    try:
        parsed = TelegramDeliveryReceipt.model_validate(receipt)
    except ValueError:
        return None
    if require_edited and parsed.edited_at_ms is None:
        return None
    return parsed
