"""Reader history, market fact writes, and delivered-card settlement persistence."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any, cast

# S608 exemptions below interpolate only closed, module-owned history predicates; all values stay bound.
from ..liquidations import LiquidationFact
from ..market_contracts import MARKET_NEWS_PUSHED_MAX, MARKET_NEWS_WINDOW_MS
from ..models import TelegramDeliveryReceipt
from ..reader_history import (
    RECENT_HISTORY_MAX,
    RECENT_HISTORY_WINDOW_MS,
    SIMILAR_HISTORY_WINDOW_MS,
    SIMILAR_TITLE_MAX,
    TARGETED_ASSET_MAX,
    TARGETED_EXACT_MAX,
    TARGETED_HISTORY_WINDOW_MS,
    ReaderHistorySnapshot,
    assemble_reader_history,
)
from ..smart_money import SmartMoneyFact
from ..source_contracts import MARKET_PROVIDER
from .feed_sql import EDITORIAL_EVENT_SQL
from .sql_values import _dumps
from .trade_projection import TradeProjectionStorage

_STORYLINE_LOCK_NAMESPACE = 0x4E455753  # 'NEWS', distinct from App session-lock namespaces.
_READER_HISTORY_PROJECTION = """
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
      JOIN news_deliveries d ON d.event_id = e.event_id AND d.kind = 'update' AND d.state = 'sent'
                            AND d.delete_state IS DISTINCT FROM 'deleted'
"""


# #582 §3.3. The News an OI card's instrument already has, in the two numbers that card prints. Here
# rather than beside the market statements because this is the *delivered-card* ledger -- the same
# rows, the same `update` / `sent` / not-deleted predicate and the same headline the reader-history
# bands above are built from -- and a second answer to "what has this reader been told" is exactly
# what one file of this SQL exists to prevent.
#
# The symbol is resolved through `news_symbol_aliases` the way the reader-history asset band resolves
# an Event's own assets: the alias's base, plus every alias of that base. An OI frame naming `9988`
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
MARKET_NEWS_PUSHED_SQL = f"""{_EQUIVALENT_SYMBOLS_CTE}{_READER_HISTORY_PROJECTION}
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

    def reader_history_revision(self, *, now_ms: int) -> tuple[int, int, str]:
        """Return a primitive CAS token for the delivered-card ledger.

        Open above `now_ms` on purpose, unlike the snapshot bands below. This answers "has the ledger
        changed since I read it", and the change it exists to catch is precisely a card that settled
        *after* the stamp the snapshot was taken at: the planner refreshes the ledger outside any transaction
        and re-reads this token inside `lock_storyline`, both at the same stamp, so an upper bound at that
        stamp would hide the racing delivery from both reads and buy the lost CAS nothing.
        """

        row = self.conn.execute(
            """
            SELECT count(*) AS row_count,
                   COALESCE(max(settled_at_ms), 0) AS newest_at_ms,
                   COALESCE(max(event_id), '') AS greatest_event_id
              FROM news_deliveries
             WHERE kind = 'update' AND state = 'sent'
               AND delete_state IS DISTINCT FROM 'deleted'
               AND settled_at_ms >= %s
            """,
            (int(now_ms) - TARGETED_HISTORY_WINDOW_MS,),
        ).fetchone()
        if row is None:  # pragma: no cover - aggregate queries always return one row
            return (0, 0, "")
        return (int(row["row_count"]), int(row["newest_at_ms"]), str(row["greatest_event_id"]))

    def reader_history(self, *, event_id: str, now_ms: int, include_targeted: bool = True) -> ReaderHistorySnapshot:
        """Reader receipt truth split into the 4 h policy ledger and the bounded semantic candidate bands.

        Every band is closed at both ends against ``now_ms`` (#651 §12): a snapshot read at a stamp contains
        only cards the reader had at that stamp, even when a delivery settled after it.

        ``reader_history_revision`` above stays open, and the asymmetry is the point: a snapshot may only
        contain cards the reader had at this stamp, while the CAS token beside it has to notice the card
        that arrives after it.
        """

        revision = self.reader_history_revision(now_ms=now_ms)
        recent = self.conn.execute(
            _READER_HISTORY_PROJECTION
            + """
             WHERE e.event_id <> %s
               AND d.settled_at_ms >= %s AND d.settled_at_ms < %s
             ORDER BY d.settled_at_ms DESC, d.event_id LIMIT %s
            """,
            (event_id, int(now_ms) - RECENT_HISTORY_WINDOW_MS, int(now_ms), RECENT_HISTORY_MAX),
        ).fetchall()
        if not include_targeted:
            return replace(assemble_reader_history(recent_rows=recent, now_ms=now_ms), ledger_revision=revision)
        current = self.conn.execute(
            "SELECT comparison_title FROM news_events WHERE event_id = %s", (event_id,)
        ).fetchone()
        comparison_title = str(current["comparison_title"] or "") if current is not None else ""

        exact = self.conn.execute(
            "WITH current_event AS ("  # noqa: S608
            " SELECT dedupe_family, comparison_fingerprint FROM news_events WHERE event_id = %s"
            ") "
            + _READER_HISTORY_PROJECTION
            + """
             CROSS JOIN current_event current
             WHERE e.event_id <> %s
               AND d.history_context ->> 'dedupe_family' = current.dedupe_family
               AND d.history_context ->> 'comparison_fingerprint' = current.comparison_fingerprint
               AND d.settled_at_ms >= %s AND d.settled_at_ms < %s
             ORDER BY d.settled_at_ms DESC, d.event_id LIMIT %s
            """,
            (
                event_id,
                event_id,
                int(now_ms) - TARGETED_HISTORY_WINDOW_MS,
                int(now_ms) - RECENT_HISTORY_WINDOW_MS,
                TARGETED_EXACT_MAX,
            ),
        ).fetchall()
        asset = self.conn.execute(
            """
            WITH current_event AS (
              SELECT event_id, dedupe_family, comparison_fingerprint
                FROM news_events WHERE event_id = %s
            ), current_bases AS (
              SELECT DISTINCT COALESCE(a.base_symbol, current_asset.symbol) AS base
                FROM current_event current
                JOIN news_event_assets current_asset ON current_asset.event_id = current.event_id
                LEFT JOIN news_symbol_aliases a ON a.alias = current_asset.symbol
            ), equivalent_symbols AS (
              SELECT base AS symbol FROM current_bases
              UNION
              SELECT a.alias FROM news_symbol_aliases a JOIN current_bases b ON b.base = a.base_symbol
            )
            """  # noqa: S608
            + _READER_HISTORY_PROJECTION
            + """
             CROSS JOIN current_event current
             WHERE e.event_id <> current.event_id
               AND NOT (
                 d.history_context ->> 'dedupe_family' = current.dedupe_family
                 AND d.history_context ->> 'comparison_fingerprint' = current.comparison_fingerprint
               )
               AND d.settled_at_ms >= %s AND d.settled_at_ms < %s
               AND EXISTS (
                 SELECT 1 FROM jsonb_array_elements_text(d.history_context -> 'canonical_assets')
                   candidate_asset(symbol)
                  WHERE candidate_asset.symbol IN (SELECT symbol FROM equivalent_symbols)
               )
             ORDER BY d.settled_at_ms DESC, d.event_id LIMIT %s
            """,
            (
                event_id,
                int(now_ms) - TARGETED_HISTORY_WINDOW_MS,
                int(now_ms) - RECENT_HISTORY_WINDOW_MS,
                TARGETED_ASSET_MAX,
            ),
        ).fetchall()
        # The title-similarity band (#491): the delivered cards of the last 24 h whose normalized title is
        # closest to this candidate's, by pg_trgm. Bounded by K rather than by delivery volume, which is what
        # the 4 h / 128 recent ledger stopped being at 38 cards an hour. Rows the recent and targeted bands
        # already selected are excluded here so every one of the K slots brings evidence those bands could not.
        # `assemble_reader_history` re-ranks the band with the Python twin of pg_trgm, so the ORDER BY is a
        # bound on what is fetched, not the ordering the Program sees.
        #
        # Shape: the 24 h delivered set is materialized first, `similarity()` is evaluated only on those rows,
        # and the wide projection (with its per-Event verdict lookup) runs for the K survivors alone. Written as
        # one join the planner evaluates `similarity()` over every `news_events` row instead — 6k today, growing
        # 2.5k a day — and then pays the verdict lookup for every 24 h row; measured 450 ms against 59 ms.
        spent = sorted(
            {str(row["event_id"]) for row in (*recent, *exact, *asset)} | {str(event_id)},
        )
        similar = (
            self.conn.execute(
                """
            WITH delivered AS MATERIALIZED (
              SELECT d.event_id, d.settled_at_ms, d.history_context, d.card
                FROM news_deliveries d
               WHERE d.kind = 'update' AND d.state = 'sent'
                 AND d.delete_state IS DISTINCT FROM 'deleted'
                 AND d.settled_at_ms >= %s AND d.settled_at_ms < %s
                 AND d.event_id <> ALL(%s)
            ), delivered_titles AS MATERIALIZED (
              SELECT e.event_id, COALESCE(w.history_context ->> 'comparison_title',
                     w.card #>> '{header,title,content}', '') AS comparison_title, w.settled_at_ms
                FROM delivered w
                JOIN news_events e ON e.event_id = w.event_id
            ), band AS MATERIALIZED (
              SELECT event_id
                FROM delivered_titles
               WHERE similarity(comparison_title, %s) > 0
               ORDER BY similarity(comparison_title, %s) DESC, settled_at_ms DESC, event_id
               LIMIT %s
            )
                """  # noqa: S608
                + _READER_HISTORY_PROJECTION
                + " JOIN band ON band.event_id = e.event_id",
                (
                    int(now_ms) - SIMILAR_HISTORY_WINDOW_MS,
                    int(now_ms),
                    spent,
                    comparison_title,
                    comparison_title,
                    SIMILAR_TITLE_MAX,
                ),
            ).fetchall()
            if comparison_title
            else []
        )
        return replace(
            assemble_reader_history(
                recent_rows=recent,
                exact_rows=exact,
                asset_rows=asset,
                similar_rows=similar,
                comparison_title=comparison_title,
                now_ms=now_ms,
            ),
            ledger_revision=revision,
        )

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

    def lock_storyline(self, storyline_key: str) -> None:
        """Transaction-scoped advisory lock on one storyline key so "read reader evidence -> decide -> insert verdict"
        is serialised per key across concurrent Triage handlers (and processes). Released at commit/rollback. The
        worker pool's 250 ms ``lock_timeout`` is raised for this transaction only: a same-key holder finishes in a
        few ms, and a waiter that gave up would re-run the whole handler including a second paid model call."""

        self.conn.execute("SET LOCAL lock_timeout = '2500ms'")
        self.conn.execute("SELECT pg_advisory_xact_lock(%s, hashtext(%s))", (_STORYLINE_LOCK_NAMESPACE, storyline_key))

    def insert_oi_signal(
        self,
        *,
        event_id: str,
        metric_version: str,
        symbol: str,
        raw_instrument: str,
        direction: str,
        oi_change_bps: int,
        oi_value_usd: int,
        whale_long_profit_bps: int,
        whale_oi_ratio_bps: int,
        observed_at_ms: int,
        received_at_ms: int,
        now_ms: int,
        provider: str,
        source_strategy_id: str | None,
        source_contract_version: str | None,
        measurement_window_ms: int | None,
        measurement_definition: str,
        source_item_id: str,
        source_venue: str | None,
        ingest_mode: str = "live",
    ) -> None:
        """Append one parsed frame to the OI ledger. Idempotent on the Item that produced it.

        The uniqueness key is `(source_item_id, metric_version)` (#553): one provider record parsed
        under one metric version is one observation, whatever else has happened to it. `event_id`
        remains the published identity a Trading Case files its answer under -- an opaque source
        string, derived from the Item, and no longer a claim that a News Event exists.

        The three source-contract columns still travel together or not at all (#265): a window with no
        identity behind it is a number nobody can audit, and `NULL` is the honest record of a frame
        whose measurement interval could not be proven. A default of five minutes here would make
        every unprovable frame claim to be a 5-minute measurement.

        Every row this writer appends is `historical = false`, which is the column's default: a fact
        arriving through admission is one this process received. The reconstructed rows are the
        migration's, written by its own statement, and no live path may mark a fact as rebuilt.
        """

        proven = (
            source_strategy_id is not None and source_contract_version is not None and measurement_window_ms is not None
        )
        cursor = self.conn.execute(
            """
            INSERT INTO news_oi_signals (
              event_id, metric_version, symbol, raw_instrument, direction, oi_change_bps, oi_value_usd,
              whale_long_profit_bps, whale_oi_ratio_bps, observed_at_ms, received_at_ms, created_at_ms,
              provider, source_strategy_id, source_contract_version, measurement_window_ms,
              measurement_definition, source_item_id, source_venue, available_at_ms
            ) VALUES (
              %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
              %s, %s, %s, %s, %s, %s, %s, %s
            )
            ON CONFLICT (source_item_id, metric_version) DO NOTHING
            """,
            (
                event_id,
                metric_version,
                symbol,
                raw_instrument,
                direction,
                int(oi_change_bps),
                int(oi_value_usd),
                int(whale_long_profit_bps),
                int(whale_oi_ratio_bps),
                int(observed_at_ms),
                int(received_at_ms),
                int(now_ms),
                provider,
                source_strategy_id if proven else None,
                source_contract_version if proven else None,
                int(measurement_window_ms) if proven and measurement_window_ms is not None else None,
                measurement_definition,
                source_item_id,
                source_venue,
                int(now_ms),
            ),
        )
        if cursor.rowcount:
            cast(TradeProjectionStorage, self).enqueue_trade_event(
                kind="oi",
                source_fact_key=event_id,
                source_revision=metric_version,
                payload={
                    "kind": "oi",
                    "source_event_ref": event_id,
                    "evidence_ref": source_item_id,
                    "evidence_sha": None,
                    "producer_identity": {
                        "provider": provider,
                        "strategy": source_strategy_id,
                        "contract": source_contract_version,
                    },
                    "assets": [{"symbol": symbol, "market_type": "crypto", "role": "primary"}],
                    "direction": direction,
                    "oi_change_bps": int(oi_change_bps),
                    "oi_value_usd": int(oi_value_usd),
                    "whale_long_profit_bps": int(whale_long_profit_bps),
                    "whale_oi_ratio_bps": int(whale_oi_ratio_bps),
                    "measurement_window_ms": measurement_window_ms,
                    "measurement_definition": measurement_definition,
                    "source_venue": source_venue,
                    "provider_event_at_ms": int(observed_at_ms),
                    "source_received_at_ms": int(received_at_ms),
                    "source_recorded_at_ms": int(now_ms),
                    "ingest_mode": ingest_mode,
                },
                source_recorded_at_ms=now_ms,
            )

    def insert_market_liquidation(self, *, fact: LiquidationFact, ingest_mode: str, now_ms: int) -> None:
        """Append one normalized liquidation report. Provider replays are idempotent by source key."""

        self.conn.execute(
            """
            INSERT INTO news_market_liquidations (
              source_key, item_id, fact_id, ingest_mode, provider, symbol, raw_instrument, source_venue,
              source_strategy_id, liquidated_position_side,
              forced_order_side, notional_usd, quantity, price, event_at_ms,
              received_at_ms, parser_version, provider_record_identity,
              symbol_contract_identity, position_side_semantics, quantity_semantics,
              notional_semantics, price_semantics, completeness_assumption,
              throttle_assumption, source_contract_version, source_contract_complete,
              available_at_ms, created_at_ms
            ) VALUES (
              %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
              %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            ON CONFLICT (source_key) DO NOTHING
            """,
            (
                fact.source_key,
                fact.item_id,
                fact.fact_id,
                ingest_mode,
                MARKET_PROVIDER,
                fact.symbol,
                fact.raw_instrument,
                fact.source_venue,
                fact.source_strategy_id,
                fact.liquidated_position_side,
                fact.forced_order_side,
                fact.notional_usd,
                fact.quantity,
                fact.price,
                int(fact.event_at_ms),
                int(fact.received_at_ms),
                fact.parser_version,
                fact.provider_record_identity,
                fact.symbol_contract_identity,
                fact.position_side_semantics,
                fact.quantity_semantics,
                fact.notional_semantics,
                fact.price_semantics,
                fact.completeness_assumption,
                fact.throttle_assumption,
                fact.source_contract_version,
                bool(fact.source_contract_complete),
                int(now_ms),
                int(now_ms),
            ),
        )

    def insert_market_smart_money(self, *, fact: SmartMoneyFact, ingest_mode: str, now_ms: int) -> None:
        """Append one reported account action. Provider replays are idempotent by source key.

        `reported_notional_usd` is the provider's own figure for one report. Nothing sums it: two
        reports about the same account are two reports, and no position total can be derived from a
        stream that never claims to be complete.
        """

        self.conn.execute(
            """
            INSERT INTO news_market_smart_money (
              source_key, item_id, fact_id, ingest_mode, provider, source_strategy_id,
              trader_label, account_address, source_venue, raw_instrument, symbol,
              action, position_side, reported_notional_usd, price, pnl_usd,
              event_at_ms, received_at_ms, available_at_ms, created_at_ms,
              parser_version, provider_record_identity, source_contract_version,
              notional_semantics, price_semantics, completeness_assumption
            ) VALUES (
              %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
              %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            ON CONFLICT (source_key) DO NOTHING
            """,
            (
                fact.source_key,
                fact.item_id,
                fact.fact_id,
                ingest_mode,
                MARKET_PROVIDER,
                fact.source_strategy_id,
                fact.trader_label,
                fact.account_address,
                fact.source_venue,
                fact.raw_instrument,
                fact.symbol,
                fact.action,
                fact.position_side,
                fact.reported_notional_usd,
                fact.price,
                fact.pnl_usd,
                int(fact.event_at_ms),
                int(fact.received_at_ms),
                int(now_ms),
                int(now_ms),
                fact.parser_version,
                fact.provider_record_identity,
                fact.source_contract_version,
                fact.notional_semantics,
                fact.price_semantics,
                fact.completeness_assumption,
            ),
        )

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
            UPDATE news_deliveries
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
            UPDATE news_deliveries
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
            UPDATE news_deliveries
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

    def terminalize_interrupted_deliveries(self, *, now_ms: int) -> int:
        """An unsettled send is ambiguous; remove its queue reservation without resending."""

        row = self.conn.execute(
            """
            WITH settled AS (
              UPDATE news_deliveries
                 SET state = 'ambiguous',
                     error_code = 'ambiguous_after_crash', settled_at_ms = %s
               WHERE state = 'sending' AND attempted_at_ms < %s
              RETURNING intent_id
            ), released AS (
              DELETE FROM news_delivery_queue q USING settled s
               WHERE q.intent_id = s.intent_id
              RETURNING q.intent_id
            )
            SELECT (SELECT count(*) FROM settled) AS settled, (SELECT count(*) FROM released) AS released
            """,
            (int(now_ms), int(now_ms) - 60_000),
        ).fetchone()
        return int(row["settled"] or 0)

    def terminalize_interrupted_delivery_edits(self, *, now_ms: int) -> int:
        cursor = self.conn.execute(
            """
            UPDATE news_deliveries
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
            UPDATE news_deliveries
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
