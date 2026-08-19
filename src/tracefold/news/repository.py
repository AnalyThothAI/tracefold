"""News V3 repository: facts, events, verdicts, deliveries, control, labels, and bounded reads.

Every write is idempotent by key. Callers own the transaction (worker_session / api_session).
"""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Mapping, Sequence
from typing import Any, Final

from .outcome import direction_zh, event_outcome, event_type_zh, magnitude_zh
from .timeline import event_timeline
from .triage_rules import ESCALATE_WINDOW_MS, PUSH_WINDOW_MS

_JSON_SEPARATORS = (",", ":")


# 'NEWS' — a two-int advisory-lock namespace distinct from the (0x54524644, n) session locks in app.database.
_STORYLINE_LOCK_NAMESPACE = 0x4E455753


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=_JSON_SEPARATORS, default=str)


class NewsRepository:
    def __init__(self, conn: Any) -> None:
        self.conn = conn

    # ------------------------------------------------------------------ ingest state / incidents
    def update_ingest_state(
        self,
        *,
        now_ms: int,
        connected: bool | None = None,
        last_frame_at_ms: int | None = None,
        last_publish_at_ms: int | None = None,
        last_error_code: str | None = None,
        clear_error: bool = False,
        configured_strategy_ids: Sequence[str] | None = None,
        provider_enabled_strategy_ids: Sequence[str] | None = None,
        strategy_warnings: Sequence[str] | None = None,
    ) -> None:
        self.conn.execute(
            """
            UPDATE news_ingest_state
               SET connected = COALESCE(%s, connected),
                   last_frame_at_ms = COALESCE(%s, last_frame_at_ms),
                   last_publish_at_ms = COALESCE(%s, last_publish_at_ms),
                   last_error_code = CASE WHEN %s THEN NULL ELSE COALESCE(%s, last_error_code) END,
                   configured_strategy_ids = COALESCE(%s::jsonb, configured_strategy_ids),
                   provider_enabled_strategy_ids = COALESCE(%s::jsonb, provider_enabled_strategy_ids),
                   strategy_warnings = COALESCE(%s::jsonb, strategy_warnings),
                   updated_at_ms = GREATEST(updated_at_ms, %s)
             WHERE singleton_key = 'opennews'
            """,
            (
                connected,
                last_frame_at_ms,
                last_publish_at_ms,
                bool(clear_error),
                last_error_code,
                _dumps(list(configured_strategy_ids)) if configured_strategy_ids is not None else None,
                _dumps(list(provider_enabled_strategy_ids)) if provider_enabled_strategy_ids is not None else None,
                _dumps(list(strategy_warnings)) if strategy_warnings is not None else None,
                int(now_ms),
            ),
        )

    def update_broker_snapshot(self, *, snapshot: Mapping[str, Any], now_ms: int) -> None:
        self.conn.execute(
            """
            UPDATE news_ingest_state SET broker_snapshot = %s::jsonb, updated_at_ms = GREATEST(updated_at_ms, %s)
             WHERE singleton_key = 'opennews'
            """,
            (_dumps({**dict(snapshot), "observed_at_ms": int(now_ms)}), int(now_ms)),
        )

    def open_incident(
        self, *, cause_class: str, now_ms: int, planned: bool = False, close_code: int | None = None
    ) -> int:
        row = self.conn.execute(
            """
            SELECT incident_id FROM news_opennews_incidents
             WHERE closed_at_ms IS NULL AND cause_class = %s
             ORDER BY incident_id DESC LIMIT 1
            """,
            (cause_class,),
        ).fetchone()
        if row is not None:
            return int(row["incident_id"])
        row = self.conn.execute(
            """
            INSERT INTO news_opennews_incidents (
              cause_class, opened_at_ms, planned, close_code, recovery_status, created_at_ms, updated_at_ms
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING incident_id
            """,
            (
                cause_class,
                int(now_ms),
                bool(planned),
                close_code,
                "not_applicable" if cause_class in {"broker_backpressure", "triage_circuit_open"} else "pending",
                int(now_ms),
                int(now_ms),
            ),
        ).fetchone()
        return int(row["incident_id"])

    def close_open_incidents(self, *, cause_classes: Sequence[str] | None, now_ms: int) -> int:
        if cause_classes is None:
            cursor = self.conn.execute(
                """
                UPDATE news_opennews_incidents
                   SET closed_at_ms = %s, recovery_to_at_ms = COALESCE(recovery_to_at_ms, %s),
                       updated_at_ms = %s
                 WHERE closed_at_ms IS NULL
                """,
                (int(now_ms), int(now_ms), int(now_ms)),
            )
        else:
            cursor = self.conn.execute(
                """
                UPDATE news_opennews_incidents
                   SET closed_at_ms = %s, recovery_to_at_ms = COALESCE(recovery_to_at_ms, %s),
                       updated_at_ms = %s
                 WHERE closed_at_ms IS NULL AND cause_class = ANY(%s)
                """,
                (int(now_ms), int(now_ms), int(now_ms), list(cause_classes)),
            )
        return int(cursor.rowcount or 0)

    def pending_recovery_incidents(self, *, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT incident_id, cause_class, opened_at_ms, closed_at_ms, recovery_from_at_ms, recovery_to_at_ms
              FROM news_opennews_incidents
             WHERE recovery_status = 'pending' AND closed_at_ms IS NOT NULL
             ORDER BY incident_id
             LIMIT %s
            """,
            (int(limit),),
        ).fetchall()
        return [dict(r) for r in rows]

    def complete_recovery(
        self,
        *,
        incident_id: int,
        status: str,
        recovered_count: int,
        error_code: str | None,
        recovery_from_at_ms: int | None,
        recovery_to_at_ms: int | None,
        now_ms: int,
    ) -> None:
        self.conn.execute(
            """
            UPDATE news_opennews_incidents
               SET recovery_status = %s, recovered_count = recovered_count + %s, last_error_code = %s,
                   recovery_from_at_ms = COALESCE(%s, recovery_from_at_ms),
                   recovery_to_at_ms = COALESCE(%s, recovery_to_at_ms), updated_at_ms = %s
             WHERE incident_id = %s
            """,
            (
                status,
                int(recovered_count),
                error_code,
                recovery_from_at_ms,
                recovery_to_at_ms,
                int(now_ms),
                int(incident_id),
            ),
        )

    def open_incidents(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT incident_id, cause_class, opened_at_ms, planned FROM news_opennews_incidents"
            " WHERE closed_at_ms IS NULL ORDER BY incident_id"
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ items
    def upsert_item(
        self,
        *,
        item_id: str,
        source_id: str,
        source_item_key: str,
        title: str,
        raw_first_line: str,
        description: str,
        canonical_url: str | None,
        reporting_origin: str,
        published_at_ms: int,
        observed_at_ms: int,
        provider_metadata: Mapping[str, Any],
        strategy_ids: Sequence[str],
        ingest_mode: str,
        trace_id: str,
        now_ms: int,
    ) -> bool:
        """Insert or merge provenance. Returns True when the Item is new."""

        row = self.conn.execute(
            """
            INSERT INTO news_items (
              item_id, source_id, source_item_key, title, raw_first_line, description, canonical_url,
              reporting_origin, published_at_ms, observed_at_ms, provider_metadata, provenance,
              first_ingest_mode, trace_id, created_at_ms, updated_at_ms
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s, %s)
            ON CONFLICT (item_id) DO UPDATE SET
              provenance = (
                SELECT COALESCE(jsonb_agg(DISTINCT value ORDER BY value), '[]'::jsonb)
                  FROM jsonb_array_elements_text(news_items.provenance || EXCLUDED.provenance) AS t(value)
              ),
              updated_at_ms = GREATEST(news_items.updated_at_ms, EXCLUDED.updated_at_ms)
            RETURNING (xmax = 0) AS inserted
            """,
            (
                item_id,
                source_id,
                source_item_key,
                title,
                raw_first_line,
                description,
                canonical_url,
                reporting_origin,
                int(published_at_ms),
                int(observed_at_ms),
                _dumps(dict(provider_metadata)),
                _dumps(sorted(set(strategy_ids))),
                ingest_mode,
                trace_id,
                int(now_ms),
                int(now_ms),
            ),
        ).fetchone()
        return bool(row["inserted"])

    # ------------------------------------------------------------------ events
    def find_exact_event(self, *, family: str, fingerprint: str, now_ms: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT event_id, opened_at_ms, expires_at_ms, admission, published_at_ms
              FROM news_events
             WHERE family = %s AND comparison_fingerprint = %s AND expires_at_ms > %s
             ORDER BY opened_at_ms ASC LIMIT 1
            """,
            (family, fingerprint, int(now_ms)),
        ).fetchone()
        return dict(row) if row else None

    def find_band_candidates(self, *, family: str, band_keys: Sequence[str], now_ms: int) -> list[dict[str, Any]]:
        pairs = [(index, key) for index, key in enumerate(band_keys)]
        if not pairs:
            return []
        rows = self.conn.execute(
            """
            WITH hits AS (
              SELECT DISTINCT b.event_id
                FROM news_event_bands b
                JOIN unnest(%s::smallint[], %s::text[]) AS q(band_index, band_key)
                  ON q.band_index = b.band_index AND q.band_key = b.band_key
               WHERE b.family = %s AND b.expires_at_ms > %s
            )
            SELECT e.event_id, e.comparison_title, e.leader_title, e.opened_at_ms, e.grounded_assets
              FROM news_events e JOIN hits ON hits.event_id = e.event_id
             ORDER BY e.opened_at_ms ASC
             LIMIT 25
            """,
            ([p[0] for p in pairs], [p[1] for p in pairs], family, int(now_ms)),
        ).fetchall()
        return [dict(r) for r in rows]

    def insert_event(
        self,
        *,
        event_id: str,
        leader_item_id: str,
        family: str,
        comparison_fingerprint: str,
        comparison_title: str,
        leader_title: str,
        opened_at_ms: int,
        expires_at_ms: int,
        admission: str,
        priority: str,
        provider_score: float | None,
        engine_type: str,
        asset_class: str,
        grounded_assets: Sequence[str],
        watchlist_hits: Sequence[str],
        macro_lexicon: bool,
        storyline_key: str,
        context_line: str,
        ingest_mode: str,
        trace_id: str,
        band_keys: Sequence[str],
        now_ms: int,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO news_events (
              event_id, leader_item_id, family, comparison_fingerprint, comparison_title, leader_title,
              opened_at_ms, last_member_at_ms, expires_at_ms, member_count, admission, priority,
              provider_score_max, engine_type, asset_class, grounded_assets, watchlist_hits, macro_lexicon,
              storyline_key, context_line, ingest_mode, trace_id, created_at_ms, updated_at_ms
            ) VALUES (
              %s, %s, %s, %s, %s, %s, %s, %s, %s, 1, %s, %s, %s, %s, %s,
              %s::jsonb, %s::jsonb, %s, %s, %s, %s, %s, %s, %s
            )
            """,
            (
                event_id,
                leader_item_id,
                family,
                comparison_fingerprint,
                comparison_title,
                leader_title,
                int(opened_at_ms),
                int(opened_at_ms),
                int(expires_at_ms),
                admission,
                priority,
                provider_score,
                engine_type,
                asset_class,
                _dumps(list(grounded_assets)),
                _dumps(list(watchlist_hits)),
                bool(macro_lexicon),
                storyline_key,
                context_line,
                ingest_mode,
                trace_id,
                int(now_ms),
                int(now_ms),
            ),
        )
        self.conn.execute(
            """
            INSERT INTO news_event_members (event_id, item_id, joined_at_ms, match_kind, jaccard_estimate)
            VALUES (%s, %s, %s, 'leader', NULL) ON CONFLICT DO NOTHING
            """,
            (event_id, leader_item_id, int(opened_at_ms)),
        )
        if band_keys:
            self.conn.execute(
                """
                INSERT INTO news_event_bands (band_index, band_key, event_id, family, expires_at_ms)
                SELECT q.band_index, q.band_key, %s, %s, %s
                  FROM unnest(%s::smallint[], %s::text[]) AS q(band_index, band_key)
                ON CONFLICT DO NOTHING
                """,
                (event_id, family, int(expires_at_ms), list(range(len(band_keys))), list(band_keys)),
            )
        for symbol in grounded_assets:
            self.conn.execute(
                """
                INSERT INTO news_event_assets (symbol, event_id, market_type, opened_at_ms)
                VALUES (%s, %s, NULL, %s) ON CONFLICT DO NOTHING
                """,
                (symbol.upper().replace("XYZ-", ""), event_id, int(opened_at_ms)),
            )

    def add_member(
        self,
        *,
        event_id: str,
        item_id: str,
        joined_at_ms: int,
        match_kind: str,
        jaccard_estimate: float | None,
        provider_score: float | None,
        now_ms: int,
    ) -> bool:
        cursor = self.conn.execute(
            """
            INSERT INTO news_event_members (event_id, item_id, joined_at_ms, match_kind, jaccard_estimate)
            VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING
            """,
            (event_id, item_id, int(joined_at_ms), match_kind, jaccard_estimate),
        )
        if not cursor.rowcount:
            return False
        self.conn.execute(
            """
            UPDATE news_events
               SET member_count = member_count + 1,
                   last_member_at_ms = GREATEST(last_member_at_ms, %s),
                   provider_score_max = GREATEST(COALESCE(provider_score_max, 0), COALESCE(%s, 0)),
                   updated_at_ms = %s
             WHERE event_id = %s
            """,
            (int(joined_at_ms), provider_score, int(now_ms), event_id),
        )
        return True

    def mark_event_published(self, *, event_id: str, now_ms: int) -> None:
        self.conn.execute(
            "UPDATE news_events SET published_at_ms = COALESCE(published_at_ms, %s), updated_at_ms = %s"
            " WHERE event_id = %s",
            (int(now_ms), int(now_ms), event_id),
        )

    def unpublished_candidates(self, *, older_than_ms: int, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT event_id FROM news_events
             WHERE published_at_ms IS NULL AND admission = 'candidate' AND opened_at_ms <= %s
             ORDER BY opened_at_ms LIMIT %s
            """,
            (int(older_than_ms), int(limit)),
        ).fetchall()
        return [dict(r) for r in rows]

    def upgrade_event_admission(
        self,
        *,
        event_id: str,
        admission: str,
        priority: str,
        asset_class: str,
        grounded_assets: Sequence[str],
        watchlist_hits: Sequence[str],
        macro_lexicon: bool,
        now_ms: int,
    ) -> None:
        """A later, stronger member re-gated a suppressed Event: record the new Gate facts in place (idempotent)."""

        row = self.conn.execute(
            """
            UPDATE news_events
               SET admission = %s, priority = %s, asset_class = %s, grounded_assets = %s::jsonb,
                   watchlist_hits = %s::jsonb, macro_lexicon = %s, updated_at_ms = %s
             WHERE event_id = %s
             RETURNING opened_at_ms
            """,
            (
                admission,
                priority,
                asset_class,
                _dumps(list(grounded_assets)),
                _dumps(list(watchlist_hits)),
                bool(macro_lexicon),
                int(now_ms),
                event_id,
            ),
        ).fetchone()
        opened_at_ms = int(row["opened_at_ms"]) if row else int(now_ms)
        for symbol in grounded_assets:
            self.conn.execute(
                """
                INSERT INTO news_event_assets (symbol, event_id, market_type, opened_at_ms)
                VALUES (%s, %s, NULL, %s) ON CONFLICT DO NOTHING
                """,
                (symbol.upper().replace("XYZ-", ""), event_id, opened_at_ms),
            )

    def set_storyline_key(self, *, event_id: str, storyline_key: str, now_ms: int) -> None:
        """Triage refined the storyline (final key from verdict primaries/scope); windows use this key from now on."""

        self.conn.execute(
            "UPDATE news_events SET storyline_key = %s, updated_at_ms = %s WHERE event_id = %s AND storyline_key <> %s",
            (storyline_key[:120], int(now_ms), event_id, storyline_key[:120]),
        )

    def set_context_line(self, *, event_id: str, context_line: str, followup_of: str | None, now_ms: int) -> None:
        self.conn.execute(
            "UPDATE news_events SET context_line = %s, followup_of = COALESCE(%s, followup_of), updated_at_ms = %s"
            " WHERE event_id = %s",
            (context_line[:400], followup_of, int(now_ms), event_id),
        )

    def event_card(self, event_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT e.*, i.description AS leader_description, i.canonical_url AS leader_url, i.reporting_origin,
                   i.provider_metadata, i.provenance, i.published_at_ms AS leader_published_at_ms,
                   i.raw_first_line
              FROM news_events e JOIN news_items i ON i.item_id = e.leader_item_id
             WHERE e.event_id = %s
            """,
            (event_id,),
        ).fetchone()
        return dict(row) if row else None

    def event_status(self, *, storyline_key: str, now_ms: int) -> dict[str, Any]:
        row = self.conn.execute(
            """
            WITH pushed AS (
              SELECT v.event_id, v.created_at_ms,
                     (v.verdict ->> 'magnitude')::int AS magnitude,
                     v.verdict ->> 'direction' AS direction
                FROM news_verdicts v JOIN news_events e ON e.event_id = v.event_id
               WHERE v.stage = 'triage' AND v.final_decision IN ('push', 'escalate')
                 AND e.storyline_key = %s AND v.created_at_ms >= %s
            )
            SELECT
              (SELECT count(*) FROM pushed WHERE created_at_ms >= %s) AS pushed_2h,
              (SELECT count(*) FROM pushed) AS pushed_4h,
              (SELECT COALESCE(max(magnitude), 0) FROM pushed WHERE created_at_ms >= %s) AS max_magnitude_2h,
              (SELECT COALESCE(max(magnitude), 0) FROM pushed) AS max_magnitude_4h,
              (SELECT COALESCE(array_agg(DISTINCT direction), '{}') FROM pushed
                WHERE created_at_ms >= %s) AS directions_2h,
              (SELECT COALESCE(array_agg(DISTINCT direction), '{}') FROM pushed) AS directions_4h,
              (SELECT %s - max(created_at_ms) FROM pushed) AS last_push_ago_ms,
              (SELECT count(*) FROM news_events WHERE storyline_key = %s AND opened_at_ms >= %s) AS events_2h
            """,
            (
                storyline_key,
                int(now_ms) - ESCALATE_WINDOW_MS,
                int(now_ms) - PUSH_WINDOW_MS,
                int(now_ms) - PUSH_WINDOW_MS,
                int(now_ms) - PUSH_WINDOW_MS,
                int(now_ms),
                storyline_key,
                int(now_ms) - PUSH_WINDOW_MS,
            ),
        ).fetchone()
        return dict(row) if row else {}

    def told_ledger(
        self, *, now_ms: int, window_ms: int, limit: int, prefer_key: str | None = None, prefer_limit: int = 8
    ) -> list[dict[str, Any]]:
        """Cards the reader received in the window: push/escalate verdicts whose first card was not terminalised
        (a Feishu failure, sender unavailable, paused lane, hourly cap or crash never reached the reader; degraded
        fallbacks are excluded too — their placeholder headline is not a card the reader can recognise). The newest
        ``limit`` overall plus, when ``prefer_key`` is given, the newest ``prefer_limit`` on that storyline (which
        may be older than the global window's newest); newest first, one row per event. The consumer trims for the
        status bar (``told_ledger_for_prompt``) and compares the event-id set for staleness."""

        base = """
            SELECT v.event_id, v.created_at_ms AS at_ms, e.storyline_key,
                   (v.verdict ->> 'magnitude')::int AS magnitude, v.verdict ->> 'direction' AS direction,
                   v.verdict ->> 'headline_zh' AS headline_zh
              FROM news_verdicts v
              JOIN news_events e ON e.event_id = v.event_id
              LEFT JOIN news_deliveries d ON d.event_id = v.event_id AND d.kind = 'first'
             WHERE v.stage = 'triage' AND v.final_decision IN ('push', 'escalate') AND NOT v.degraded
               AND v.created_at_ms >= %s AND COALESCE(d.state, '') <> 'terminal'
        """
        since = int(now_ms) - int(window_ms)
        rows = self.conn.execute(base + " ORDER BY v.created_at_ms DESC LIMIT %s", (since, int(limit))).fetchall()
        merged = {str(r["event_id"]): dict(r) for r in rows}
        if prefer_key:
            same = self.conn.execute(
                base + " AND e.storyline_key = %s ORDER BY v.created_at_ms DESC LIMIT %s",
                (since, prefer_key, int(prefer_limit)),
            ).fetchall()
            for r in same:
                merged.setdefault(str(r["event_id"]), dict(r))
        return sorted(merged.values(), key=lambda r: -int(r["at_ms"]))

    def lock_storyline(self, storyline_key: str) -> None:
        """Transaction-scoped advisory lock on one storyline key so "read window facts -> decide -> insert verdict"
        is serialised per key across concurrent Triage handlers (and processes). Released at commit/rollback. The
        worker pool's 250 ms ``lock_timeout`` is raised for this transaction only: a same-key holder finishes in a
        few ms, and a waiter that gave up would re-run the whole handler including a second paid model call."""

        self.conn.execute("SET LOCAL lock_timeout = '2500ms'")
        self.conn.execute("SELECT pg_advisory_xact_lock(%s, hashtext(%s))", (_STORYLINE_LOCK_NAMESPACE, storyline_key))

    # ------------------------------------------------------------------ verdicts
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
        model: str | None,
        prompt_version: str | None,
        degraded: bool,
        error_code: str | None,
        trace: Mapping[str, Any],
        now_ms: int,
    ) -> bool:
        cursor = self.conn.execute(
            """
            INSERT INTO news_verdicts (
              event_id, stage, policy_version, model_decision, rule_baseline_decision, final_decision, override_rule,
              throttled_by, verdict, model, prompt_version, degraded, error_code, trace, created_at_ms
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s::jsonb, %s)
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
                model,
                prompt_version,
                bool(degraded),
                error_code,
                _dumps(dict(trace)),
                int(now_ms),
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

    def sent_count_since(self, *, since_ms: int) -> int:
        row = self.conn.execute(
            "SELECT count(*) AS n FROM news_deliveries WHERE state = 'sent' AND settled_at_ms >= %s", (int(since_ms),)
        ).fetchone()
        return int(row["n"]) if row else 0

    def terminalize_interrupted_deliveries(self, *, now_ms: int) -> int:
        cursor = self.conn.execute(
            """
            UPDATE news_deliveries SET state = 'terminal', error_code = 'ambiguous_after_crash', settled_at_ms = %s
             WHERE state = 'sending' AND attempted_at_ms < %s
            """,
            (int(now_ms), int(now_ms) - 60_000),
        )
        return int(cursor.rowcount or 0)

    # ------------------------------------------------------------------ control
    def read_control(self, *, now_ms: int) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT paused, mutes, updated_at_ms FROM news_control_state WHERE singleton_key = 'current'"
        ).fetchone()
        if row is None:
            return {"paused": False, "mutes": []}
        mutes = [m for m in (row["mutes"] or []) if int(m.get("until_ms") or 0) > int(now_ms)]
        return {"paused": bool(row["paused"]), "mutes": mutes, "updated_at_ms": int(row["updated_at_ms"])}

    def write_control(self, *, paused: bool | None, mutes: Sequence[Mapping[str, Any]] | None, now_ms: int) -> None:
        self.conn.execute(
            """
            UPDATE news_control_state
               SET paused = COALESCE(%s, paused), mutes = COALESCE(%s::jsonb, mutes), updated_at_ms = %s
             WHERE singleton_key = 'current'
            """,
            (paused, _dumps([dict(m) for m in mutes]) if mutes is not None else None, int(now_ms)),
        )

    # ------------------------------------------------------------------ janitor / marks / labels
    def expire_bands(self, *, now_ms: int) -> int:
        cursor = self.conn.execute("DELETE FROM news_event_bands WHERE expires_at_ms < %s", (int(now_ms),))
        return int(cursor.rowcount or 0)

    def purge_before(self, *, cutoff_ms: int) -> int:
        cursor = self.conn.execute("DELETE FROM news_items WHERE observed_at_ms < %s", (int(cutoff_ms),))
        return int(cursor.rowcount or 0)

    def insert_label(
        self, *, event_id: str, label_version: str, source: str, label: Mapping[str, Any], now_ms: int
    ) -> bool:
        cursor = self.conn.execute(
            """
            INSERT INTO news_event_labels (event_id, label_version, source, label, created_at_ms)
            VALUES (%s, %s, %s, %s::jsonb, %s) ON CONFLICT DO NOTHING
            """,
            (event_id, label_version, source, _dumps(dict(label)), int(now_ms)),
        )
        return bool(cursor.rowcount)

    def labels_for_event(self, event_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT label_version, source, label, created_at_ms FROM news_event_labels"
            " WHERE event_id = %s ORDER BY created_at_ms",
            (event_id,),
        ).fetchall()
        return [
            {
                "label_version": r["label_version"],
                "source": r["source"],
                "label": dict(r["label"] or {}),
                "created_at_ms": int(r["created_at_ms"]),
            }
            for r in rows
        ]

    # ------------------------------------------------------------------ reads (Serve)
    def list_feed(
        self,
        *,
        family: str | None,
        admission: str | None,
        priority: str | None,
        decision: str | None,
        symbol: str | None,
        q: str | None,
        sort: str,
        limit: int,
        cursor: str | None,
        outcome: str | None = None,
        hours: int | None = None,
        now_ms: int | None = None,
    ) -> dict[str, Any]:
        cursor_opened, cursor_id = _decode_cursor(cursor)
        params: list[Any] = []
        where = ["e.ingest_mode IN ('live', 'recovery')"]
        window_hours = int(hours) if hours else None
        if window_hours:
            # The response echoes `hours`, never the wall-clock bound, so an unchanged page keeps its ETag.
            since_ms = int(now_ms if now_ms is not None else time.time() * 1000) - window_hours * 3600_000
            where.append("e.opened_at_ms >= %s")
            params.append(since_ms)
        if outcome in _OUTCOME_GROUP_SQL:
            where.append(_OUTCOME_GROUP_SQL[outcome])
        if family:
            where.append("e.family = %s")
            params.append(family)
        if admission:
            where.append("e.admission = %s")
            params.append(admission)
        if priority:
            where.append("e.priority = %s")
            params.append(priority)
        if symbol:
            where.append("EXISTS (SELECT 1 FROM news_event_assets a WHERE a.event_id = e.event_id AND a.symbol = %s)")
            params.append(symbol.upper())
        if q:
            where.append("(e.search_doc @@ plainto_tsquery('simple', %s) OR e.leader_title ILIKE %s)")
            params.extend([q, f"%{q}%"])
        if decision:
            where.append("t.final_decision = %s")
            params.append(decision)
        if cursor_opened is not None:
            where.append("(e.opened_at_ms, e.event_id) < (%s, %s)")
            params.extend([cursor_opened, cursor_id])
        order = (
            "e.priority = 'high' DESC, e.opened_at_ms DESC, e.event_id DESC"
            if sort == "priority"
            else "e.opened_at_ms DESC, e.event_id DESC"
        )
        rows = self.conn.execute(
            f"""
            SELECT e.event_id, e.family, e.leader_title, e.opened_at_ms, e.last_member_at_ms, e.member_count,
                   e.admission, e.priority, e.provider_score_max, e.engine_type, e.asset_class, e.grounded_assets,
                   e.watchlist_hits, e.storyline_key, e.context_line, e.published_at_ms, e.ingest_mode,
                   i.canonical_url AS leader_url, i.reporting_origin, i.provenance,
                   t.final_decision, t.override_rule, t.throttled_by, t.degraded AS triage_degraded,
                   t.error_code AS triage_error_code,
                   t.verdict ->> 'direction' AS direction, (t.verdict ->> 'magnitude')::int AS magnitude,
                   t.verdict ->> 'event_type' AS event_type, t.verdict ->> 'headline_zh' AS headline_zh,
                   t.verdict ->> 'scope' AS scope, t.verdict ->> 'title_zh' AS title_zh,
                   d.state AS delivery_state, d.settled_at_ms AS delivered_at_ms, d.error_code AS delivery_error_code
              FROM news_events e
              JOIN news_items i ON i.item_id = e.leader_item_id
              LEFT JOIN LATERAL (
                SELECT * FROM news_verdicts v WHERE v.event_id = e.event_id AND v.stage = 'triage'
                 ORDER BY v.created_at_ms DESC LIMIT 1
              ) t ON true
              LEFT JOIN news_deliveries d ON d.event_id = e.event_id AND d.kind = 'first'
             WHERE {" AND ".join(where)}
             ORDER BY {order}
             LIMIT %s
            """,
            (*params, int(limit) + 1),
        ).fetchall()
        items = [_feed_row(dict(r)) for r in rows[: int(limit)]]
        next_cursor = None
        if len(rows) > int(limit):
            last = rows[int(limit) - 1]
            next_cursor = _encode_cursor(int(last["opened_at_ms"]), str(last["event_id"]))
        return {
            "events": items,
            "next_cursor": next_cursor,
            "filters": {
                "family": family,
                "admission": admission,
                "priority": priority,
                "decision": decision,
                "symbol": symbol,
                "q": q,
                "sort": sort,
                "limit": int(limit),
                "outcome": outcome if outcome in _OUTCOME_GROUP_SQL else None,
                "hours": window_hours,
            },
        }

    def event_detail(self, event_id: str) -> dict[str, Any] | None:
        card = self.event_card(event_id)
        if card is None:
            return None
        members = self.conn.execute(
            """
            SELECT m.item_id, m.joined_at_ms, m.match_kind, m.jaccard_estimate, i.title, i.canonical_url,
                   i.reporting_origin, i.published_at_ms, i.provenance, i.description
              FROM news_event_members m JOIN news_items i ON i.item_id = m.item_id
             WHERE m.event_id = %s ORDER BY m.joined_at_ms, m.item_id
            """,
            (event_id,),
        ).fetchall()
        verdicts = self.conn.execute(
            "SELECT * FROM news_verdicts WHERE event_id = %s ORDER BY created_at_ms", (event_id,)
        ).fetchall()
        deliveries = self.conn.execute(
            "SELECT * FROM news_deliveries WHERE event_id = %s ORDER BY created_at_ms", (event_id,)
        ).fetchall()
        event = _event_public(card)
        member_rows = [
            {
                "item_id": r["item_id"],
                "title": r["title"],
                "url": r["canonical_url"],
                "reporting_origin": r["reporting_origin"],
                "published_at_ms": int(r["published_at_ms"]),
                "joined_at_ms": int(r["joined_at_ms"]),
                "match_kind": r["match_kind"],
                "jaccard_estimate": r["jaccard_estimate"],
                "provenance": list(r["provenance"] or []),
                "description": r["description"],
            }
            for r in members
        ]
        verdict_rows = [_verdict_public(dict(r)) for r in verdicts]
        delivery_rows = [
            {
                "kind": r["kind"],
                "state": r["state"],
                "error_code": r["error_code"],
                "attempted_at_ms": int(r["attempted_at_ms"]),
                "settled_at_ms": r["settled_at_ms"],
                "receipt": r["receipt"],
            }
            for r in deliveries
        ]
        outcome, timeline = event_timeline(
            event=event, members=member_rows, verdicts=verdict_rows, deliveries=delivery_rows
        )
        return {
            "event": event,
            "outcome": outcome.as_dict(),
            "timeline": timeline,
            "members": member_rows,
            "verdicts": verdict_rows,
            "deliveries": delivery_rows,
            "labels": self.labels_for_event(event_id),
        }

    def status_snapshot(self, *, now_ms: int) -> dict[str, Any]:
        ingest = self.conn.execute("SELECT * FROM news_ingest_state WHERE singleton_key = 'opennews'").fetchone()
        incidents = self.open_incidents()
        day_ago = int(now_ms) - 24 * 3600_000
        hour_ago = int(now_ms) - 3600_000
        pipeline = self.conn.execute(
            """
            SELECT
              (SELECT count(*) FROM news_events WHERE opened_at_ms >= %s) AS events_1h,
              (SELECT count(*) FROM news_events WHERE opened_at_ms >= %s) AS events_24h,
              (SELECT count(*) FROM news_events WHERE opened_at_ms >= %s AND admission = 'candidate') AS candidates_24h,
              (SELECT count(*) FROM news_verdicts WHERE stage = 'triage' AND created_at_ms >= %s) AS triage_24h,
              (SELECT count(*) FROM news_verdicts
                WHERE stage = 'triage' AND degraded AND created_at_ms >= %s) AS triage_degraded_24h,
              (SELECT count(*) FROM news_verdicts
                WHERE stage = 'triage' AND final_decision IN ('push','escalate')
                  AND created_at_ms >= %s) AS decided_push_24h,
              (SELECT count(*) FROM news_verdicts
                WHERE stage = 'triage' AND final_decision = 'throttled' AND created_at_ms >= %s) AS throttled_24h,
              (SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY (trace ->> 'latency_ms')::double precision)
                 FROM news_verdicts
                WHERE stage = 'triage' AND created_at_ms >= %s AND trace ? 'latency_ms') AS triage_p50_ms,
              (SELECT percentile_cont(0.95) WITHIN GROUP (ORDER BY (trace ->> 'latency_ms')::double precision)
                 FROM news_verdicts
                WHERE stage = 'triage' AND created_at_ms >= %s AND trace ? 'latency_ms') AS triage_p95_ms,
              (SELECT percentile_cont(0.95) WITHIN GROUP (ORDER BY (trace ->> 'queue_lag_ms')::double precision)
                 FROM news_verdicts
                WHERE stage = 'triage' AND created_at_ms >= %s AND trace ? 'queue_lag_ms') AS queue_lag_p95_ms,
              (SELECT count(*) FROM news_verdicts
                WHERE stage = 'triage' AND created_at_ms >= %s
                  AND COALESCE((trace ->> 'reasked_after_told_change')::boolean, false)) AS reasked_24h,
              (SELECT count(*) FROM news_verdicts
                WHERE stage = 'triage' AND created_at_ms >= %s
                  AND COALESCE((trace ->> 'novelty_defaulted')::boolean, false)) AS novelty_defaulted_24h
            """,
            (hour_ago, *([day_ago] * 11)),
        ).fetchone()
        delivery = self.conn.execute(
            """
            SELECT
              (SELECT count(*) FROM news_deliveries WHERE state = 'sent' AND settled_at_ms >= %s) AS sent_24h,
              (SELECT count(*) FROM news_deliveries WHERE state = 'sent' AND settled_at_ms >= %s) AS sent_1h,
              (SELECT count(*) FROM news_deliveries WHERE state = 'terminal' AND settled_at_ms >= %s) AS terminal_24h,
              (SELECT error_code FROM news_deliveries WHERE state = 'terminal'
                ORDER BY settled_at_ms DESC NULLS LAST LIMIT 1) AS last_error_code,
              (SELECT percentile_cont(0.95)
                 WITHIN GROUP (ORDER BY (d.settled_at_ms - i.observed_at_ms)::double precision)
                 FROM news_deliveries d JOIN news_events e ON e.event_id = d.event_id
                 JOIN news_items i ON i.item_id = e.leader_item_id
                WHERE d.state = 'sent' AND d.kind = 'first' AND d.settled_at_ms >= %s) AS e2e_p95_ms
            """,
            (day_ago, hour_ago, day_ago, day_ago),
        ).fetchone()
        control = self.read_control(now_ms=now_ms)
        funnel = self._funnel_24h(day_ago=day_ago)
        return {
            "ingest": {
                "connected": bool(ingest["connected"]) if ingest else False,
                "last_frame_at_ms": ingest["last_frame_at_ms"] if ingest else None,
                "last_publish_at_ms": ingest["last_publish_at_ms"] if ingest else None,
                "last_error_code": ingest["last_error_code"] if ingest else None,
                "configured_strategy_ids": list(ingest["configured_strategy_ids"] or []) if ingest else [],
                "provider_enabled_strategy_ids": (
                    list(ingest["provider_enabled_strategy_ids"])
                    if ingest and ingest["provider_enabled_strategy_ids"] is not None
                    else None
                ),
                "strategy_warnings": list(ingest["strategy_warnings"] or []) if ingest else [],
                "open_incidents": [
                    {
                        "incident_id": int(r["incident_id"]),
                        "cause_class": r["cause_class"],
                        "opened_at_ms": int(r["opened_at_ms"]),
                        "planned": bool(r["planned"]),
                    }
                    for r in incidents
                ],
            },
            "pipeline": {
                **{
                    k: (float(v) if isinstance(v, float) else (int(v) if v is not None else None))
                    for k, v in dict(pipeline or {}).items()
                },
                **funnel,
            },
            "broker": dict(ingest["broker_snapshot"] or {}) if ingest else {},
            "delivery": {
                "sent_24h": int(delivery["sent_24h"] or 0) if delivery else 0,
                "sent_1h": int(delivery["sent_1h"] or 0) if delivery else 0,
                "terminal_24h": int(delivery["terminal_24h"] or 0) if delivery else 0,
                "last_error_code": delivery["last_error_code"] if delivery else None,
                "e2e_p95_ms": float(delivery["e2e_p95_ms"])
                if delivery and delivery["e2e_p95_ms"] is not None
                else None,
            },
            "control": control,
        }

    def _funnel_24h(self, *, day_ago: int) -> dict[str, Any]:
        """Where the last 24 h of Events went, by named reason: Gate admissions, decide() rules, storyline keys."""

        suppressed = self.conn.execute(
            """
            SELECT admission, count(*) AS n FROM news_events
             WHERE opened_at_ms >= %s AND admission NOT IN ('candidate', 'listing_deterministic')
             GROUP BY admission ORDER BY n DESC
            """,
            (day_ago,),
        ).fetchall()
        # One pass over the last 24 h of Triage verdicts; the four named maps are folded from it in Python.
        verdict_groups = self.conn.execute(
            """
            SELECT final_decision, COALESCE(override_rule, 'unknown') AS rule,
                   COALESCE(throttled_by, 'unknown') AS key, degraded, COALESCE(error_code, 'unknown') AS code,
                   count(*) AS n
              FROM news_verdicts
             WHERE stage = 'triage' AND created_at_ms >= %s
             GROUP BY 1, 2, 3, 4, 5
            """,
            (day_ago,),
        ).fetchall()
        dropped: dict[str, int] = {}
        throttled: dict[str, int] = {}
        pushed_by_rule: dict[str, int] = {}
        degraded_by_code: dict[str, int] = {}
        for row in verdict_groups:
            n = int(row["n"])
            final = str(row["final_decision"])
            if final == "drop":
                dropped[str(row["rule"])] = dropped.get(str(row["rule"]), 0) + n
            elif final == "throttled":
                throttled[str(row["key"])] = throttled.get(str(row["key"]), 0) + n
            elif final in {"push", "escalate"}:
                pushed_by_rule[str(row["rule"])] = pushed_by_rule.get(str(row["rule"]), 0) + n
            if row["degraded"]:
                degraded_by_code[str(row["code"])] = degraded_by_code.get(str(row["code"]), 0) + n
        missed = self.conn.execute(
            """
            SELECT count(DISTINCT l.event_id) AS n
              FROM news_event_labels l JOIN news_events e ON e.event_id = l.event_id
             WHERE l.created_at_ms >= %s AND l.label ->> 'label' = 'missed'
            """,
            (day_ago,),
        ).fetchone()
        totals = self.conn.execute(
            """
            SELECT count(*) AS events,
                   count(*) FILTER (WHERE admission IN ('candidate', 'listing_deterministic')) AS admitted
              FROM news_events WHERE opened_at_ms >= %s
            """,
            (day_ago,),
        ).fetchone()
        events = int(totals["events"] or 0) if totals else 0
        admitted = int(totals["admitted"] or 0) if totals else 0
        return {
            "suppressed_by_reason": {str(r["admission"]): int(r["n"]) for r in suppressed},
            "dropped_by_rule": dict(sorted(dropped.items(), key=lambda kv: -kv[1])),
            "throttled_by_key": dict(sorted(throttled.items(), key=lambda kv: -kv[1])[:10]),
            "pushed_by_rule": dict(sorted(pushed_by_rule.items(), key=lambda kv: -kv[1])),
            "triage_degraded_by_code_24h": dict(sorted(degraded_by_code.items(), key=lambda kv: -kv[1])),
            "labeled_missed_24h": int(missed["n"] or 0) if missed else 0,
            "candidate_share_24h": round(admitted / events, 4) if events else None,
        }


# Feed task tabs (mirrors OUTCOME_GROUP in outcome.py, expressed over the feed's joined rows;
# tests/integration/test_news_v3_pipeline.py asserts the three predicates partition the feed exactly like
# event_outcome().group over the fixture corpus):
# pushed = the first card was sent; pending = still moving (not yet triaged, or decided push and not yet settled);
# held = everything that stopped short of a sent card (gate, drop, throttle, fallback drop, delivery failure).
_PENDING_CORE_SQL: Final = (
    "e.admission IN ('candidate', 'listing_deterministic')"
    " AND (t.final_decision IS NULL"
    "      OR (t.final_decision IN ('push', 'escalate') AND (d.state IS NULL OR d.state = 'sending')))"
)
_OUTCOME_GROUP_SQL: Final = {
    "pushed": "d.state = 'sent'",
    "pending": f"COALESCE(d.state, '') <> 'sent' AND ({_PENDING_CORE_SQL})",
    "held": f"COALESCE(d.state, '') <> 'sent' AND NOT ({_PENDING_CORE_SQL})",
}


def _decode_cursor(cursor: str | None) -> tuple[int | None, str | None]:
    if not cursor:
        return None, None
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii") + b"=" * (-len(cursor) % 4)).decode("utf-8")
        opened, event_id = raw.split("|", 1)
        return int(opened), event_id
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("news_feed_cursor_invalid") from exc


def _encode_cursor(opened_at_ms: int, event_id: str) -> str:
    return base64.urlsafe_b64encode(f"{opened_at_ms}|{event_id}".encode()).decode("ascii").rstrip("=")


def _event_public(card: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "event_id": card["event_id"],
        "family": card["family"],
        "leader_title": card["leader_title"],
        "leader_url": card.get("leader_url"),
        "leader_description": card.get("leader_description", ""),
        "reporting_origin": card.get("reporting_origin", ""),
        "opened_at_ms": int(card["opened_at_ms"]),
        "last_member_at_ms": int(card["last_member_at_ms"]),
        "member_count": int(card["member_count"]),
        "admission": card["admission"],
        "priority": card["priority"],
        "provider_score_max": card.get("provider_score_max"),
        "engine_type": card["engine_type"],
        "asset_class": card["asset_class"],
        "grounded_assets": list(card.get("grounded_assets") or []),
        "watchlist_hits": list(card.get("watchlist_hits") or []),
        "macro_lexicon": bool(card.get("macro_lexicon")),
        "storyline_key": card.get("storyline_key", ""),
        "context_line": card.get("context_line", ""),
        "published_at_ms": card.get("published_at_ms"),
        "ingest_mode": card["ingest_mode"],
        "provenance": list(card.get("provenance") or []),
    }


def _feed_row(row: Mapping[str, Any]) -> dict[str, Any]:
    triage = (
        {
            "final_decision": row["final_decision"],
            "override_rule": row.get("override_rule"),
            "throttled_by": row.get("throttled_by"),
            "degraded": bool(row.get("triage_degraded")),
            "error_code": row.get("triage_error_code"),
            "direction": row.get("direction"),
            "magnitude": row.get("magnitude"),
            "event_type": row.get("event_type"),
            "scope": row.get("scope"),
            "headline_zh": row.get("headline_zh"),
            "title_zh": row.get("title_zh"),
            "direction_zh": direction_zh(row.get("direction")),
            "magnitude_zh": magnitude_zh(row.get("magnitude")),
            "event_type_zh": event_type_zh(row.get("event_type")),
        }
        if row.get("final_decision")
        else None
    )
    delivery = (
        {
            "state": row["delivery_state"],
            "settled_at_ms": row.get("delivered_at_ms"),
            "error_code": row.get("delivery_error_code"),
        }
        if row.get("delivery_state")
        else None
    )
    outcome = event_outcome(
        admission=row.get("admission"), published_at_ms=row.get("published_at_ms"), triage=triage, delivery=delivery
    )
    return {
        **_event_public(row),
        "title_zh": (row.get("title_zh") or None),
        "outcome": outcome.as_dict(),
        "triage": triage,
        "delivery": delivery,
    }


def _verdict_public(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "stage": row["stage"],
        "policy_version": row["policy_version"],
        "model_decision": row.get("model_decision"),
        "rule_baseline_decision": row["rule_baseline_decision"],
        "final_decision": row["final_decision"],
        "override_rule": row.get("override_rule"),
        "throttled_by": row.get("throttled_by"),
        "verdict": dict(row.get("verdict") or {}),
        "model": row.get("model"),
        "prompt_version": row.get("prompt_version"),
        "degraded": bool(row.get("degraded")),
        "error_code": row.get("error_code"),
        "trace": dict(row.get("trace") or {}),
        "published_at_ms": row.get("published_at_ms"),
        "created_at_ms": int(row["created_at_ms"]),
    }


__all__ = ["NewsRepository"]
