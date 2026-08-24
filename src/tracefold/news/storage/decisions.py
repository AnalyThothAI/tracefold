"""Told ledger, OI signals, verdicts, and delivery settlement persistence."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

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
           e.comparison_fingerprint, e.family,
           v.verdict ->> 'event_type' AS event_type,
           (v.verdict ->> 'magnitude')::int AS magnitude,
           v.verdict ->> 'direction' AS direction,
           COALESCE(NULLIF(d.card #>> '{header,title,content}', ''), v.verdict ->> 'headline_zh') AS headline_zh,
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
      JOIN LATERAL (
        SELECT candidate.*
          FROM (
            -- Keep the Event-led primary-key lookup separate from the newest-route sort. Otherwise PostgreSQL
            -- can walk the stage/time index once per Event and filter every other Event on each walk.
            SELECT scoped.* FROM news_verdicts scoped
             WHERE scoped.event_id = e.event_id
               AND scoped.stage = 'triage'
               AND scoped.final_decision IN ('push', 'escalate')
             OFFSET 0
          ) candidate
         ORDER BY candidate.created_at_ms DESC, candidate.policy_version DESC
         LIMIT 1
      ) v ON true
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
            " SELECT family, comparison_fingerprint FROM news_events WHERE event_id = %s"
            ") "
            + _READER_HISTORY_PROJECTION
            + """
             CROSS JOIN current_event current
             WHERE e.event_id <> %s
               AND e.family = current.family
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
              SELECT event_id, family, comparison_fingerprint
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
            """
            + _READER_HISTORY_PROJECTION
            + """
             CROSS JOIN current_event current
             WHERE e.event_id <> current.event_id
               AND NOT (
                 e.family = current.family
                 AND e.comparison_fingerprint = current.comparison_fingerprint
               )
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

    def told_ledger(self, *, now_ms: int, window_ms: int, limit: int) -> list[dict[str, Any]]:
        """Cards proven to have reached the reader in the window: the newest ``limit``, newest first, one row per
        event.

        Only ``news_deliveries(kind='first', state='sent')`` is reader truth. A decision, missing row, sending
        row, terminal failure, or ambiguous settle is not.

        This is one bounded ledger with two readers: ``decide()`` measures duplicate evidence against all of it,
        and the told-context selector ranks it against the candidate and shows the model the top rows. There is
        no ``prefer_key`` any more — reserving same-storyline rows in the query was the old selector's job, and
        the selector now does it against the candidate's own facts instead of its preliminary key alone.

        The projection is the selector's input contract: whatever it can rank on has to be here, and
        ``TOLD_SELECTOR_SHA256`` pins this exact column list."""

        rows = self.conn.execute(
            """
            SELECT v.event_id, d.settled_at_ms AS at_ms, e.storyline_key, e.comparison_title,
                   v.verdict ->> 'event_type' AS event_type,
                   (v.verdict ->> 'magnitude')::int AS magnitude, v.verdict ->> 'direction' AS direction,
                   COALESCE(NULLIF(d.card #>> '{header,title,content}', ''), v.verdict ->> 'headline_zh')
                     AS headline_zh,
                   -- The instruments the card was about. `headline_zh` is Chinese reader prose with
                   -- parenthesised tickers stripped by contract, so it cannot answer "same asset?".
                   COALESCE(e.grounded_assets, '[]'::jsonb) AS grounded_assets,
                   COALESCE(v.verdict -> 'assets', '[]'::jsonb) AS assets
              FROM news_verdicts v
              JOIN news_events e ON e.event_id = v.event_id
              JOIN news_deliveries d ON d.event_id = v.event_id AND d.kind = 'first' AND d.state = 'sent'
             WHERE v.stage = 'triage' AND v.final_decision IN ('push', 'escalate')
               AND d.settled_at_ms >= %s
             ORDER BY d.settled_at_ms DESC LIMIT %s
            """,
            (int(now_ms) - int(window_ms), int(limit)),
        ).fetchall()
        return [dict(row) for row in rows]

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
            "SELECT count(*)::int AS n FROM news_oi_signals "
            "WHERE metric_version = %s AND symbol = %s "
            "AND observed_at_ms > %s AND observed_at_ms <= %s AND event_id <> %s "
            "AND whale_oi_ratio_bps > %s AND abs(oi_change_bps) >= %s",
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
            "SELECT event_id, metric_version, symbol, direction, oi_change_bps, oi_value_usd, "
            "whale_long_profit_bps, whale_oi_ratio_bps, observed_at_ms, rank_in_window "
            "FROM news_oi_signals WHERE event_id = %s AND metric_version = %s",
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
    ) -> None:
        """Append one parsed frame to the rank ledger. Idempotent; the decision lives in the verdict."""

        self.conn.execute(
            """
            INSERT INTO news_oi_signals (
              event_id, metric_version, symbol, direction, oi_change_bps, oi_value_usd,
              whale_long_profit_bps, whale_oi_ratio_bps, observed_at_ms, rank_in_window, created_at_ms
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
            ),
        )

    def insert_verdict(
        self,
        *,
        event_id: str,
        stage: str,
        policy_version: str,
        model_decision: str | None,
        rule_baseline_decision: str,
        final_decision: str,
        override_rule: str | None,
        throttled_by: str | None,
        verdict: Mapping[str, Any],
        editorial: Mapping[str, Any],
        scored_judgment_sha256: str,
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
              event_id, stage, policy_version, model_decision, rule_baseline_decision, final_decision, override_rule,
              throttled_by, verdict, editorial, scored_judgment_sha256, runtime_manifest_sha,
              model, program_version, program_sha256, degraded, error_code, trace, created_at_ms,
              evidence_version, evidence_sha256, focus_fact_id
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s,
                      %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            """,
            (
                event_id,
                stage,
                policy_version,
                model_decision,
                rule_baseline_decision,
                final_decision,
                override_rule,
                throttled_by,
                _dumps(dict(verdict)),
                _dumps(dict(editorial)),
                scored_judgment_sha256,
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
            "SELECT * FROM news_verdicts WHERE event_id = %s AND stage = %s AND policy_version = %s",
            (event_id, stage, policy_version),
        ).fetchone()
        return dict(row) if row else None

    def latest_verdict(self, *, event_id: str, stage: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM news_verdicts WHERE event_id = %s AND stage = %s ORDER BY created_at_ms DESC LIMIT 1",
            (event_id, stage),
        ).fetchone()
        return dict(row) if row else None

    def mark_verdict_published(self, *, event_id: str, stage: str, policy_version: str, now_ms: int) -> None:
        self.conn.execute(
            """
            UPDATE news_verdicts SET published_at_ms = COALESCE(published_at_ms, %s)
             WHERE event_id = %s AND stage = %s AND policy_version = %s
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
