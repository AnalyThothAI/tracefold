"""Told ledger, OI signals, verdicts, and delivery settlement persistence."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..models import TelegramDeliveryReceipt
from ..reader_history import (
    RECENT_HISTORY_MAX,
    RECENT_HISTORY_WINDOW_MS,
    TARGETED_ASSET_MAX,
    TARGETED_EXACT_MAX,
    TARGETED_HISTORY_WINDOW_MS,
    ReaderHistorySnapshot,
    assemble_reader_history,
)
from .sql_values import _dumps

_STORYLINE_LOCK_NAMESPACE = 0x4E455753  # 'NEWS', distinct from App session-lock namespaces.
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
      FROM news_current_events_v1 e
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


class DecisionStorage:
    conn: Any

    def reader_history(self, *, event_id: str, now_ms: int, include_targeted: bool = True) -> ReaderHistorySnapshot:
        """Reader receipt truth split into the 4 h policy ledger and bounded 48 h semantic candidates."""

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
            return assemble_reader_history(recent_rows=recent, now_ms=now_ms)

        exact = self.conn.execute(
            "WITH current_event AS ("
            " SELECT dedupe_family, comparison_fingerprint FROM news_current_events_v1 WHERE event_id = %s"
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
                FROM news_current_events_v1 WHERE event_id = %s
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
            """
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
        return assemble_reader_history(recent_rows=recent, exact_rows=exact, asset_rows=asset, now_ms=now_ms)

    def lock_storyline(self, storyline_key: str) -> None:
        """Transaction-scoped advisory lock on one storyline key so "read reader evidence -> decide -> insert verdict"
        is serialised per key across concurrent Triage handlers (and processes). Released at commit/rollback. The
        worker pool's 250 ms ``lock_timeout`` is raised for this transaction only: a same-key holder finishes in a
        few ms, and a waiter that gave up would re-run the whole handler including a second paid model call."""

        self.conn.execute("SET LOCAL lock_timeout = '2500ms'")
        self.conn.execute("SELECT pg_advisory_xact_lock(%s, hashtext(%s))", (_STORYLINE_LOCK_NAMESPACE, storyline_key))

    def count_recent_eligible_oi_signals(
        self,
        *,
        symbol: str,
        metric_version: str,
        since_ms: int,
        before_ms: int,
        whale_oi_ratio_above_bps: int,
        oi_change_at_least_bps: int,
        exclude_event_id: str = "",
    ) -> int:
        """Count eligible *other* frames for this symbol in ``(since_ms, before_ms]``.

        Filtering happens before ``count(*)`` so any number of ineligible rows cannot hide older
        eligible ones. ``before_ms`` is the judged frame's publication time; the inclusive upper bound
        preserves provider frames sharing one millisecond, while ``exclude_event_id`` keeps a redelivery
        out of its own history.
        """

        row = self.conn.execute(
            "SELECT count(*)::int AS n FROM news_oi_signals signal "
            "JOIN news_current_events_v1 event ON event.event_id = signal.event_id "
            "WHERE signal.metric_version = %s AND signal.symbol = %s "
            "AND signal.observed_at_ms > %s AND signal.observed_at_ms <= %s AND signal.event_id <> %s "
            "AND signal.whale_oi_ratio_bps > %s AND abs(signal.oi_change_bps) >= %s",
            (
                metric_version,
                symbol,
                int(since_ms),
                int(before_ms),
                exclude_event_id,
                int(whale_oi_ratio_above_bps),
                int(oi_change_at_least_bps),
            ),
        ).fetchone()
        return int(row["n"] if row is not None else 0)

    def oi_signal(self, *, event_id: str, metric_version: str) -> dict[str, Any] | None:
        """The code-verified OI row that may ground its deterministic reader card."""

        row = self.conn.execute(
            "SELECT signal.event_id, signal.metric_version, signal.symbol, signal.direction, "
            "signal.oi_change_bps, signal.oi_value_usd, signal.whale_long_profit_bps, "
            "signal.whale_oi_ratio_bps, signal.observed_at_ms, signal.rank_in_window, "
            "signal.source_strategy_id, signal.source_contract_version, signal.measurement_window_ms "
            "FROM news_oi_signals signal "
            "JOIN news_current_events_v1 event ON event.event_id = signal.event_id "
            "WHERE signal.event_id = %s AND signal.metric_version = %s",
            (event_id, metric_version),
        ).fetchone()
        return dict(row) if row is not None else None

    def insert_oi_signal(
        self,
        *,
        event_id: str,
        metric_version: str,
        symbol: str,
        direction: str,
        oi_change_bps: int,
        oi_value_usd: int,
        whale_long_profit_bps: int,
        whale_oi_ratio_bps: int,
        observed_at_ms: int,
        rank_in_window: int,
        now_ms: int,
        source_strategy_id: str | None = None,
        source_contract_version: str | None = None,
        measurement_window_ms: int | None = None,
    ) -> None:
        """Append one parsed frame to the rank ledger. Idempotent; the decision lives in the verdict.

        The three source-contract columns travel together or not at all (#265): a window with no
        identity behind it is a number nobody can audit, and `NULL` is the honest record of a frame
        whose measurement interval this judge could not prove. A default of five minutes here would
        make every unprovable frame claim to be a 5-minute measurement, which is the whole failure the
        columns exist to prevent.
        """

        proven = (
            source_strategy_id is not None and source_contract_version is not None and measurement_window_ms is not None
        )
        self.conn.execute(
            """
            INSERT INTO news_oi_signals (
              event_id, metric_version, symbol, direction, oi_change_bps, oi_value_usd,
              whale_long_profit_bps, whale_oi_ratio_bps, observed_at_ms, rank_in_window, created_at_ms,
              source_strategy_id, source_contract_version, measurement_window_ms
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (event_id, metric_version) DO NOTHING
            """,
            (
                event_id,
                metric_version,
                symbol,
                direction,
                int(oi_change_bps),
                int(oi_value_usd),
                int(whale_long_profit_bps),
                int(whale_oi_ratio_bps),
                int(observed_at_ms),
                int(rank_in_window),
                int(now_ms),
                source_strategy_id if proven else None,
                source_contract_version if proven else None,
                int(measurement_window_ms) if proven and measurement_window_ms is not None else None,
            ),
        )

    def insert_market_liquidation(
        self,
        *,
        source_key: str,
        item_id: str,
        fact_id: str,
        ingest_mode: str,
        symbol: str,
        venue: str,
        liquidated_position_side: str,
        forced_order_side: str,
        notional_usd: Any,
        quantity: Any | None,
        price: Any,
        event_at_ms: int,
        received_at_ms: int,
        parser_version: str,
        provider_record_identity: str,
        symbol_contract_identity: str,
        position_side_semantics: str,
        quantity_semantics: str,
        notional_semantics: str,
        price_semantics: str,
        completeness_assumption: str,
        throttle_assumption: str,
        source_contract_version: str,
        source_contract_complete: bool,
        now_ms: int,
    ) -> None:
        """Append one normalized Strategy 2000 fact. Provider replays are idempotent by source key."""

        self.conn.execute(
            """
            INSERT INTO news_market_liquidations (
              source_key, item_id, fact_id, ingest_mode, symbol, venue, liquidated_position_side,
              forced_order_side, notional_usd, quantity, price, event_at_ms,
              received_at_ms, parser_version, provider_record_identity,
              symbol_contract_identity, position_side_semantics, quantity_semantics,
              notional_semantics, price_semantics, completeness_assumption,
              throttle_assumption, source_contract_version, source_contract_complete, created_at_ms
            ) VALUES (
              %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
              %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            ON CONFLICT (source_key) DO NOTHING
            """,
            (
                source_key,
                item_id,
                fact_id,
                ingest_mode,
                symbol,
                venue,
                liquidated_position_side,
                forced_order_side,
                notional_usd,
                quantity,
                price,
                int(event_at_ms),
                int(received_at_ms),
                parser_version,
                provider_record_identity,
                symbol_contract_identity,
                position_side_semantics,
                quantity_semantics,
                notional_semantics,
                price_semantics,
                completeness_assumption,
                throttle_assumption,
                source_contract_version,
                bool(source_contract_complete),
                int(now_ms),
            ),
        )

    def market_liquidation(self, *, item_id: str, fact_id: str, parser_version: str) -> dict[str, Any] | None:
        """The typed fact behind one deterministic liquidation verdict."""

        row = self.conn.execute(
            """
            SELECT source_key, item_id, fact_id, ingest_mode, symbol, venue, liquidated_position_side,
                   forced_order_side, notional_usd, quantity, price, event_at_ms,
                   received_at_ms, parser_version, provider_record_identity,
                   symbol_contract_identity, position_side_semantics, quantity_semantics,
                   notional_semantics, price_semantics, completeness_assumption,
                   throttle_assumption, source_contract_version, source_contract_complete
              FROM news_market_liquidations
             WHERE item_id = %s AND fact_id = %s AND parser_version = %s
            """,
            (item_id, fact_id, parser_version),
        ).fetchone()
        return dict(row) if row is not None else None

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
        model_editorial: Mapping[str, Any] | None,
        judgment_sha256: str,
        runtime_manifest_sha: str,
        model: str | None,
        program_version: str,
        program_sha256: str,
        degraded: bool,
        error_code: str | None,
        trace: Mapping[str, Any],
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
                _dumps(dict(verdict)),
                _dumps(dict(model_editorial)) if model_editorial is not None else None,
                judgment_sha256,
                runtime_manifest_sha,
                model,
                program_version,
                program_sha256,
                bool(degraded),
                error_code,
                _dumps(dict(trace)),
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

    def mark_verdict_published(self, *, event_id: str, stage: str, policy_version: str, now_ms: int) -> None:
        self.conn.execute(
            """
            UPDATE news_verdicts SET published_at_ms = COALESCE(published_at_ms, %s)
             WHERE event_id = %s AND stage = %s AND policy_version = %s
               AND judgment_contract_version = 'news_judgment_v2'
            """,
            (int(now_ms), event_id, stage, policy_version),
        )

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

    def begin_delivery_delete(
        self,
        *,
        event_id: str,
        kind: str,
        evidence: Mapping[str, Any],
        reason: str,
        receipt: Mapping[str, Any],
        now_ms: int,
    ) -> bool:
        """Persist why one provider message is safe to delete before calling the provider."""

        parsed = _telegram_receipt(receipt)
        normalized_reason = str(reason or "")
        if parsed is None or not normalized_reason or len(normalized_reason) > 200:
            return False
        cursor = self.conn.execute(
            """
            UPDATE news_deliveries
               SET delete_state = 'deleting', delete_evidence = %s::jsonb, delete_reason = %s,
                   delete_error_code = NULL, delete_attempted_at_ms = %s, delete_settled_at_ms = NULL
             WHERE event_id = %s AND kind = %s AND state = 'sent'
               AND receipt ->> 'provider' = %s
               AND receipt ->> 'message_id' = %s
               AND receipt ->> 'pushed_at_ms' = %s
               AND receipt ->> 'target_sha256' = %s
               AND delete_state IS NULL
               AND (edit_state IS NULL OR edit_state = 'edited')
            """,
            (
                _dumps(dict(evidence)),
                normalized_reason,
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

    def settle_delivery_delete(
        self,
        *,
        event_id: str,
        kind: str,
        receipt: Mapping[str, Any],
        now_ms: int,
    ) -> bool:
        parsed = _telegram_receipt(receipt, require_deleted=True)
        if parsed is None:
            return False
        cursor = self.conn.execute(
            """
            UPDATE news_deliveries
               SET receipt = %s::jsonb, delete_state = 'deleted', delete_error_code = NULL,
                   delete_settled_at_ms = %s
             WHERE event_id = %s AND kind = %s AND state = 'sent' AND delete_state = 'deleting'
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

    def mark_delivery_delete_ambiguous(
        self,
        *,
        event_id: str,
        kind: str,
        receipt: Mapping[str, Any],
        error_code: str,
        now_ms: int,
    ) -> bool:
        parsed = _telegram_receipt(receipt)
        normalized_error = str(error_code or "")
        if parsed is None or not normalized_error or len(normalized_error) > 160:
            return False
        cursor = self.conn.execute(
            """
            UPDATE news_deliveries
               SET delete_state = 'ambiguous', delete_error_code = %s, delete_settled_at_ms = %s
             WHERE event_id = %s AND kind = %s AND state = 'sent' AND delete_state = 'deleting'
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

    def terminalize_interrupted_delivery_deletes(self, *, now_ms: int) -> int:
        cursor = self.conn.execute(
            """
            UPDATE news_deliveries
               SET delete_state = 'ambiguous', delete_error_code = 'delete_ambiguous_after_crash',
                   delete_settled_at_ms = %s
             WHERE delete_state = 'deleting'
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

    def terminalize_stale_delivery_deletes(self, *, now_ms: int) -> int:
        cursor = self.conn.execute(
            """
            UPDATE news_deliveries
               SET delete_state = 'ambiguous', delete_error_code = 'delete_settlement_unavailable',
                   delete_settled_at_ms = %s
             WHERE delete_state = 'deleting' AND delete_attempted_at_ms < %s
            """,
            (int(now_ms), int(now_ms) - 60_000),
        )
        return int(cursor.rowcount or 0)


def _telegram_receipt(
    receipt: Mapping[str, Any],
    *,
    require_edited: bool = False,
    require_deleted: bool = False,
) -> TelegramDeliveryReceipt | None:
    try:
        parsed = TelegramDeliveryReceipt.model_validate(receipt)
    except ValueError:
        return None
    if require_edited and parsed.edited_at_ms is None:
        return None
    if require_deleted and parsed.deleted_at_ms is None:
        return None
    return parsed
