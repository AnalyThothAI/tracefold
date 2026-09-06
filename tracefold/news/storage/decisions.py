"""Told ledger, market fact writes, verdicts, and delivery settlement persistence."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

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

_STORYLINE_LOCK_NAMESPACE = 0x4E455753  # 'NEWS', distinct from App session-lock namespaces.
_HANDOFF_STATE_LIMIT = 1_000
UNPUBLISHED_VERDICT_CANDIDATES_SQL = """
    SELECT v.event_id, v.policy_version, v.created_at_ms, e.queue_priority, e.trace_id
      FROM news_verdicts v
      JOIN news_events e ON e.event_id = v.event_id
      JOIN news_event_evidence_snapshots evidence
        ON evidence.event_id = v.event_id
       AND evidence.evidence_version = v.evidence_version
       AND evidence.evidence_sha256 = v.evidence_sha256
       AND evidence.provenance = 'observed'
       AND evidence.snapshot ->> 'schema_version' = 'news_event_evidence_v3'
     WHERE v.stage = 'triage'
       AND v.judgment_contract_version = 'news_judgment_v2'
       AND v.final_decision IN ('push', 'escalate')
       AND v.published_at_ms IS NULL
       AND v.created_at_ms <= %s AND v.created_at_ms >= %s
     ORDER BY v.created_at_ms, v.event_id, v.policy_version LIMIT %s
"""
_VERDICT_HANDOFF_STATE_SQL = """
    WITH pending AS MATERIALIZED (
      SELECT v.created_at_ms
        FROM news_verdicts v
        JOIN news_events e ON e.event_id = v.event_id
        JOIN news_event_evidence_snapshots evidence
          ON evidence.event_id = v.event_id
         AND evidence.evidence_version = v.evidence_version
         AND evidence.evidence_sha256 = v.evidence_sha256
         AND evidence.provenance = 'observed'
         AND evidence.snapshot ->> 'schema_version' = 'news_event_evidence_v3'
       WHERE v.stage = 'triage'
         AND v.judgment_contract_version = 'news_judgment_v2'
         AND v.final_decision IN ('push', 'escalate')
         AND v.published_at_ms IS NULL
         AND v.created_at_ms >= %s
       ORDER BY v.created_at_ms, v.event_id, v.policy_version
       LIMIT %s
    ), expired AS MATERIALIZED (
      SELECT v.created_at_ms
        FROM news_verdicts v
        JOIN news_events e ON e.event_id = v.event_id
        JOIN news_event_evidence_snapshots evidence
          ON evidence.event_id = v.event_id
         AND evidence.evidence_version = v.evidence_version
         AND evidence.evidence_sha256 = v.evidence_sha256
         AND evidence.provenance = 'observed'
         AND evidence.snapshot ->> 'schema_version' = 'news_event_evidence_v3'
       WHERE v.stage = 'triage'
         AND v.judgment_contract_version = 'news_judgment_v2'
         AND v.final_decision IN ('push', 'escalate')
         AND v.published_at_ms IS NULL
         AND v.created_at_ms < %s
       ORDER BY v.created_at_ms DESC, v.event_id DESC, v.policy_version DESC
       LIMIT %s
    )
    SELECT (SELECT count(*) FROM pending) AS pending,
           (SELECT min(created_at_ms) FROM pending) AS oldest_pending_at_ms,
           (SELECT count(*) FROM expired) AS expired
"""
_READER_HISTORY_PROJECTION = """
    SELECT v.event_id, d.settled_at_ms AS at_ms, e.storyline_key, e.comparison_title,
           e.comparison_fingerprint, e.dedupe_family,
           (v.verdict ->> 'magnitude')::int AS magnitude,
           v.verdict ->> 'direction' AS direction,
           COALESCE(NULLIF(d.card #>> '{header,title,content}', ''), v.verdict ->> 'headline_zh') AS headline_zh,
           v.verdict ->> 'why_zh' AS why_zh,
           COALESCE(e.grounded_assets, '[]'::jsonb) AS grounded_assets,
           COALESCE(v.verdict -> 'assets', '[]'::jsonb) AS assets,
           COALESCE(
             (SELECT jsonb_agg(base_symbol ORDER BY base_symbol)
                FROM (SELECT DISTINCT COALESCE(a.base_symbol, ea.symbol) AS base_symbol
                        FROM news_event_assets ea
                        LEFT JOIN news_symbol_aliases a ON a.alias = ea.symbol
                       WHERE ea.event_id = e.event_id) bases),
             '[]'::jsonb
           ) AS canonical_assets
      FROM news_events e
      JOIN news_deliveries d ON d.event_id = e.event_id AND d.kind = 'first' AND d.state = 'sent'
                            AND d.delete_state IS DISTINCT FROM 'deleted'
      JOIN LATERAL (
        SELECT candidate.*
          FROM (
            -- Keep the Event-led primary-key lookup separate from the newest-route sort. Otherwise PostgreSQL
            -- can walk the stage/time index once per Event and filter every other Event on each walk.
            SELECT scoped.* FROM news_verdicts scoped
             WHERE scoped.event_id = e.event_id
               AND scoped.stage = 'triage'
               AND scoped.judgment_contract_version = 'news_judgment_v2'
               AND scoped.final_decision IN ('push', 'escalate')
             OFFSET 0
          ) candidate
         ORDER BY candidate.created_at_ms DESC, candidate.policy_version DESC
         LIMIT 1
      ) v ON true
      JOIN news_event_evidence_snapshots evidence
        ON evidence.event_id = v.event_id
       AND evidence.evidence_version = v.evidence_version
       AND evidence.evidence_sha256 = v.evidence_sha256
       AND evidence.provenance = 'observed'
       AND evidence.snapshot ->> 'schema_version' = 'news_event_evidence_v3'
"""


# #582 §3.3. The News an OI card's instrument already has, in the two numbers that card prints. Here
# rather than beside the market statements because this is the *delivered-card* ledger -- the same
# rows, the same `first` / `sent` / not-deleted predicate and the same headline the reader-history
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
     ORDER BY d.settled_at_ms DESC, v.event_id
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
        """Return a primitive CAS token for the delivered-card ledger used by Triage."""

        row = self.conn.execute(
            """
            SELECT count(*) AS row_count,
                   COALESCE(max(settled_at_ms), 0) AS newest_at_ms,
                   COALESCE(max(event_id), '') AS greatest_event_id
              FROM news_deliveries
             WHERE kind = 'first' AND state = 'sent'
               AND delete_state IS DISTINCT FROM 'deleted'
               AND settled_at_ms >= %s
            """,
            (int(now_ms) - TARGETED_HISTORY_WINDOW_MS,),
        ).fetchone()
        if row is None:  # pragma: no cover - aggregate queries always return one row
            return (0, 0, "")
        return (int(row["row_count"]), int(row["newest_at_ms"]), str(row["greatest_event_id"]))

    def reader_history(self, *, event_id: str, now_ms: int, include_targeted: bool = True) -> ReaderHistorySnapshot:
        """Reader receipt truth split into the 4 h policy ledger and the bounded semantic candidate bands."""

        revision = self.reader_history_revision(now_ms=now_ms)
        recent = self.conn.execute(
            _READER_HISTORY_PROJECTION
            + """
             WHERE e.event_id <> %s
               AND d.settled_at_ms >= %s
             ORDER BY d.settled_at_ms DESC, v.event_id LIMIT %s
            """,
            (event_id, int(now_ms) - RECENT_HISTORY_WINDOW_MS, RECENT_HISTORY_MAX),
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
               AND e.dedupe_family = current.dedupe_family
               AND e.comparison_fingerprint = current.comparison_fingerprint
               AND d.settled_at_ms >= %s AND d.settled_at_ms < %s
             ORDER BY d.settled_at_ms DESC, v.event_id LIMIT %s
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
                 e.dedupe_family = current.dedupe_family
                 AND e.comparison_fingerprint = current.comparison_fingerprint
               )
               -- The targeted band asks "what *story* about this asset has the reader already been
               -- told", and a deterministic telemetry frame is a measurement rather than a story.
               -- Excluded explicitly rather than by accident (#267): these Events carried no
               -- `news_event_assets` row until the deterministic judge's own primary was recorded
               -- there, so before that they could never be candidates. Letting them in would have
               -- changed the model lane's `told` selection — up to `TARGETED_ASSET_MAX` slots of it —
               -- as a side effect of a fix to the price plane, with no measurement behind the change.
               -- The 4 h `recent` window is untouched and still shows every delivered card, telemetry
               -- included, which is where a just-pushed OI card belongs.
               AND e.admission NOT IN ('telemetry_deterministic', 'liquidation_deterministic')
               AND d.settled_at_ms >= %s AND d.settled_at_ms < %s
               AND EXISTS (
                 SELECT 1 FROM news_event_assets candidate_asset
                  WHERE candidate_asset.event_id = e.event_id
                    AND candidate_asset.symbol IN (SELECT symbol FROM equivalent_symbols)
               )
             ORDER BY d.settled_at_ms DESC, v.event_id LIMIT %s
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
              SELECT d.event_id, d.settled_at_ms
                FROM news_deliveries d
               WHERE d.kind = 'first' AND d.state = 'sent'
                 AND d.delete_state IS DISTINCT FROM 'deleted'
                 AND d.settled_at_ms >= %s
                 AND d.event_id <> ALL(%s)
            ), delivered_titles AS MATERIALIZED (
              SELECT e.event_id, e.comparison_title, w.settled_at_ms
                FROM delivered w
                JOIN news_events e ON e.event_id = w.event_id
               WHERE e.admission NOT IN ('telemetry_deterministic', 'liquidation_deterministic')
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
        return {
            "pushed": [
                {
                    "event_id": str(row["event_id"]),
                    "headline_zh": headline,
                    "at_ms": int(row["at_ms"] or 0),
                }
                for row in pushed
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
        self.conn.execute(
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

    def insert_verdict(
        self,
        *,
        event_id: str,
        stage: str,
        policy_version: str,
        judgment_contract_version: str,
        judgment_origin: str,
        rule_baseline_decision: str,
        final_decision: str,
        override_rule: str | None,
        throttled_by: str | None,
        verdict: Mapping[str, Any],
        verdict_json: str | None = None,
        model_editorial: Mapping[str, Any] | None,
        model_editorial_json: str | None = None,
        judgment_sha256: str,
        runtime_manifest_sha: str,
        model: str | None,
        program_version: str,
        program_sha256: str,
        degraded: bool,
        error_code: str | None,
        trace: Mapping[str, Any],
        trace_json: str | None = None,
        evidence_version: int,
        evidence_sha256: str,
        focus_fact_id: str,
        now_ms: int,
    ) -> bool:
        cursor = self.conn.execute(
            """
            INSERT INTO news_verdicts (
              event_id, stage, policy_version, judgment_contract_version, judgment_origin,
              rule_baseline_decision, final_decision, override_rule,
              throttled_by, verdict, editorial, scored_judgment_sha256, runtime_manifest_sha,
              model, program_version, program_sha256, degraded, error_code, trace, created_at_ms,
              evidence_version, evidence_sha256, focus_fact_id
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s,
                      %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            """,
            (
                event_id,
                stage,
                policy_version,
                judgment_contract_version,
                judgment_origin,
                rule_baseline_decision,
                final_decision,
                override_rule,
                throttled_by,
                verdict_json if verdict_json is not None else _dumps(dict(verdict)),
                (
                    model_editorial_json
                    if model_editorial_json is not None
                    else (_dumps(dict(model_editorial)) if model_editorial is not None else None)
                ),
                judgment_sha256,
                runtime_manifest_sha,
                model,
                program_version,
                program_sha256,
                bool(degraded),
                error_code,
                trace_json if trace_json is not None else _dumps(dict(trace)),
                int(now_ms),
                int(evidence_version),
                evidence_sha256,
                focus_fact_id,
            ),
        )
        return bool(cursor.rowcount)

    def get_verdict(self, *, event_id: str, stage: str, policy_version: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM news_verdicts WHERE event_id = %s AND stage = %s AND policy_version = %s "
            "AND judgment_contract_version = 'news_judgment_v2'",
            (event_id, stage, policy_version),
        ).fetchone()
        return dict(row) if row else None

    def latest_verdict(self, *, event_id: str, stage: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM news_verdicts WHERE event_id = %s AND stage = %s "
            "AND judgment_contract_version = 'news_judgment_v2' ORDER BY created_at_ms DESC LIMIT 1",
            (event_id, stage),
        ).fetchone()
        return dict(row) if row else None

    def mark_verdict_published(self, *, event_id: str, stage: str, policy_version: str, now_ms: int) -> bool:
        cursor = self.conn.execute(
            """
            UPDATE news_verdicts SET published_at_ms = %s
             WHERE event_id = %s AND stage = %s AND policy_version = %s
               AND judgment_contract_version = 'news_judgment_v2' AND published_at_ms IS NULL
            """,
            (int(now_ms), event_id, stage, policy_version),
        )
        return bool(cursor.rowcount)

    def unpublished_verdict_candidates(
        self, *, older_than_ms: int, newer_than_ms: int, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Push Verdicts whose confirmed Delivery handoff marker is still absent."""

        rows = self.conn.execute(
            UNPUBLISHED_VERDICT_CANDIDATES_SQL,
            (int(older_than_ms), int(newer_than_ms), int(limit)),
        ).fetchall()
        return [dict(row) for row in rows]

    def verdict_handoff_scan(
        self, *, older_than_ms: int, newer_than_ms: int, limit: int = 50
    ) -> tuple[list[dict[str, Any]], dict[str, int | None]]:
        return (
            self.unpublished_verdict_candidates(
                older_than_ms=older_than_ms,
                newer_than_ms=newer_than_ms,
                limit=limit,
            ),
            self._verdict_handoff_state(deadline_ms=newer_than_ms),
        )

    def _verdict_handoff_state(self, *, deadline_ms: int) -> dict[str, int | None]:
        row = self.conn.execute(
            _VERDICT_HANDOFF_STATE_SQL,
            (int(deadline_ms), _HANDOFF_STATE_LIMIT, int(deadline_ms), _HANDOFF_STATE_LIMIT),
        ).fetchone()
        return {
            "pending": int(row["pending"] or 0) if row else 0,
            "oldest_pending_at_ms": int(row["oldest_pending_at_ms"])
            if row and row["oldest_pending_at_ms"] is not None
            else None,
            "expired": int(row["expired"] or 0) if row else 0,
        }

    def begin_delivery(self, *, event_id: str, kind: str, card: Mapping[str, Any], now_ms: int) -> str:
        """Returns 'new' when this process owns the send, otherwise the existing state."""

        row = self.conn.execute(
            """
            INSERT INTO news_deliveries (event_id, kind, state, card, attempted_at_ms, created_at_ms)
            VALUES (%s, %s, 'sending', %s::jsonb, %s, %s)
            ON CONFLICT (event_id, kind) DO NOTHING
            RETURNING state
            """,
            (event_id, kind, _dumps(dict(card)), int(now_ms), int(now_ms)),
        ).fetchone()
        if row is not None:
            return "new"
        existing = self.conn.execute(
            "SELECT state FROM news_deliveries WHERE event_id = %s AND kind = %s", (event_id, kind)
        ).fetchone()
        return str(existing["state"]) if existing else "new"

    def settle_delivery(
        self,
        *,
        event_id: str,
        kind: str,
        state: str,
        receipt: Mapping[str, Any] | None,
        error_code: str | None,
        now_ms: int,
    ) -> bool:
        cursor = self.conn.execute(
            """
            UPDATE news_deliveries SET state = %s, receipt = %s::jsonb, error_code = %s, settled_at_ms = %s
             WHERE event_id = %s AND kind = %s AND state = 'sending'
            """,
            (state, _dumps(dict(receipt)) if receipt is not None else None, error_code, int(now_ms), event_id, kind),
        )
        return bool(cursor.rowcount)

    def begin_delivery_edit(
        self,
        *,
        event_id: str,
        kind: str,
        card: Mapping[str, Any],
        receipt: Mapping[str, Any],
        now_ms: int,
    ) -> bool:
        """Persist the desired replacement before mutating one provider message."""

        parsed = _telegram_receipt(receipt)
        if parsed is None:
            return False
        cursor = self.conn.execute(
            """
            UPDATE news_deliveries
               SET edit_state = 'editing', pending_card = %s::jsonb,
                   edit_error_code = NULL, edit_attempted_at_ms = %s, edit_settled_at_ms = NULL
             WHERE event_id = %s AND kind = %s AND state = 'sent'
               AND receipt ->> 'provider' = %s
               AND receipt ->> 'message_id' = %s
               AND receipt ->> 'pushed_at_ms' = %s
               AND receipt ->> 'target_sha256' = %s
               AND (edit_state IS NULL OR edit_state = 'edited')
            """,
            (
                _dumps(dict(card)),
                int(now_ms),
                event_id,
                kind,
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
        event_id: str,
        kind: str,
        receipt: Mapping[str, Any],
        now_ms: int,
    ) -> bool:
        """CAS a confirmed provider edit over its already-durable desired card."""

        parsed = _telegram_receipt(receipt, require_edited=True)
        if parsed is None:
            return False
        cursor = self.conn.execute(
            """
            UPDATE news_deliveries
               SET card = pending_card, pending_card = NULL, receipt = %s::jsonb,
                   edit_state = 'edited', edit_error_code = NULL, edit_settled_at_ms = %s
             WHERE event_id = %s AND kind = %s AND state = 'sent' AND edit_state = 'editing'
               AND receipt ->> 'provider' = %s
               AND receipt ->> 'message_id' = %s
               AND receipt ->> 'pushed_at_ms' = %s
               AND receipt ->> 'target_sha256' = %s
            """,
            (
                _dumps(parsed.canonical()),
                int(now_ms),
                event_id,
                kind,
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
        event_id: str,
        kind: str,
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
             WHERE event_id = %s AND kind = %s AND state = 'sent' AND edit_state = 'editing'
               AND receipt ->> 'provider' = %s
               AND receipt ->> 'message_id' = %s
               AND receipt ->> 'pushed_at_ms' = %s
               AND receipt ->> 'target_sha256' = %s
            """,
            (
                normalized_error,
                int(now_ms),
                event_id,
                kind,
                parsed.provider,
                str(parsed.message_id),
                str(parsed.pushed_at_ms),
                parsed.target_sha256,
            ),
        )
        return bool(cursor.rowcount)

    def delivery(self, *, event_id: str, kind: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM news_deliveries WHERE event_id = %s AND kind = %s", (event_id, kind)
        ).fetchone()
        return dict(row) if row else None

    def terminalize_interrupted_deliveries(self, *, now_ms: int) -> int:
        cursor = self.conn.execute(
            """
            UPDATE news_deliveries SET state = 'terminal', error_code = 'ambiguous_after_crash', settled_at_ms = %s
             WHERE state = 'sending' AND attempted_at_ms < %s
            """,
            (int(now_ms), int(now_ms) - 60_000),
        )
        return int(cursor.rowcount or 0)

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
