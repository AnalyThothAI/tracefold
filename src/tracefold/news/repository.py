"""News V3 repository: facts, events, verdicts, deliveries, and bounded reads.

Every write is idempotent by key. Callers own the transaction (worker_session / api_session).
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from collections.abc import Mapping, Sequence
from typing import Any, Final

from .models import ADMITTED_ADMISSIONS, ReaderReceipt, display_title
from .opennews import source_artifact_identity
from .outcome import (
    audience_zh,
    decision_zh,
    direction_zh,
    event_outcome,
    event_type_zh,
    magnitude_zh,
    novelty_zh,
    scope_zh,
)
from .timeline import event_timeline

_JSON_SEPARATORS = (",", ":")


# 'NEWS' — a two-int advisory-lock namespace distinct from the (0x54524644, n) session locks in app.database.
_STORYLINE_LOCK_NAMESPACE = 0x4E455753


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=_JSON_SEPARATORS, default=str)


_ADMITTED_SQL: Final = ", ".join(f"'{value}'" for value in sorted(ADMITTED_ADMISSIONS))
"""SQL literal for `ADMITTED_ADMISSIONS`, derived rather than repeated.

Every predicate below means the same thing — "the Gate sent this Event to Triage" — and each used to
spell the list out. A new admission then had to be found in six places by review, which is how #137's
first pass left the outbox rescue, the give-up count, the funnel and the feed's outcome tabs all
disagreeing with `event_outcome()`. The values are code-owned members of a `Literal`, never input.
"""


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
    ) -> None:
        self.conn.execute(
            """
            UPDATE news_ingest_state
               SET connected = COALESCE(%s, connected),
                   last_frame_at_ms = COALESCE(%s, last_frame_at_ms),
                   last_publish_at_ms = COALESCE(%s, last_publish_at_ms),
                   last_error_code = CASE WHEN %s THEN NULL ELSE COALESCE(%s, last_error_code) END,
                   updated_at_ms = GREATEST(updated_at_ms, %s)
             WHERE singleton_key = 'opennews'
            """,
            (
                connected,
                last_frame_at_ms,
                last_publish_at_ms,
                bool(clear_error),
                last_error_code,
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
        source_artifact_id: str = "",
    ) -> bool:
        """Insert or merge provenance. Returns True when the Item is new."""

        row = self.conn.execute(
            """
            INSERT INTO news_items (
              item_id, source_id, source_item_key, title, raw_first_line, description, canonical_url,
              reporting_origin, published_at_ms, observed_at_ms, provider_metadata, provenance,
              first_ingest_mode, trace_id, created_at_ms, updated_at_ms, source_artifact_id
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s, %s, %s)
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
                source_artifact_id,
            ),
        ).fetchone()
        return bool(row["inserted"])

    # ------------------------------------------------------------------ events
    def find_artifact_event(
        self,
        *,
        source_artifact_id: str,
        family: str,
        fingerprint: str,
        item_id: str,
        opened_after_ms: int,
    ) -> dict[str, Any] | None:
        """The Event another Item built from this same source artifact and this same fact (#154).

        The fingerprint is part of the key, not decoration. Without it a digest split into four FactUnits would
        collapse into one Event the second time the provider sent it, because all four units share the artifact.
        With it, unit *k* can only join unit *k*.

        What the artifact id buys is the right to ignore the two guards the text-derived path needs: the
        three-token `shareable` floor (a tweet titled `What a coincidence!` scores below it and so was never
        looked up at all — the provider sent it twice, four seconds apart, under two URL spellings, and the
        reader got two cards) and the 12 h family window (`opened_after_ms` is the caller's longer horizon).
        Both guards exist because *text* similarity is evidence; artifact identity is not evidence, it is the
        platform's own primary key.
        """

        if not source_artifact_id:
            return None
        # Only an Event that can still reach a reader may absorb a live frame. The 12 h family window used to
        # bound this implicitly; a 7-day horizon does not, and joining a `recovery` Event — which is in
        # `_REGATE_ADMISSIONS`, so it can never be upgraded and never delivers — would swallow the card
        # silently. A suppressed Event is excluded for the same reason.
        row = self.conn.execute(
            """
            SELECT e.event_id, e.opened_at_ms, e.expires_at_ms, e.admission, e.published_at_ms
              FROM news_items i
              JOIN news_event_members m ON m.item_id = i.item_id
              JOIN news_events e ON e.event_id = m.event_id
             WHERE i.source_artifact_id = %s AND i.item_id <> %s
               AND e.family = %s AND e.comparison_fingerprint = %s
               AND e.opened_at_ms >= %s
               AND e.admission = ANY(%s)
             ORDER BY e.opened_at_ms ASC LIMIT 1
            """,
            (
                source_artifact_id,
                item_id,
                family,
                fingerprint,
                int(opened_after_ms),
                sorted(ADMITTED_ADMISSIONS),
            ),
        ).fetchone()
        return dict(row) if row else None

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
        focus_fact_id: str,
        focus_fact_text: str,
        focus_fact_context: str,
        focus_fact_method: str,
        focus_span_start: int,
        focus_span_end: int,
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
              focus_fact_id, focus_fact_text, focus_fact_context, focus_fact_method, focus_span_start, focus_span_end,
              opened_at_ms, last_member_at_ms, expires_at_ms, member_count, admission, priority,
              provider_score_max, engine_type, asset_class, grounded_assets, watchlist_hits, macro_lexicon,
              storyline_key, context_line, ingest_mode, trace_id, created_at_ms, updated_at_ms
            ) VALUES (
              %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 1, %s, %s, %s, %s, %s,
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
                focus_fact_id,
                focus_fact_text,
                focus_fact_context,
                focus_fact_method,
                int(focus_span_start),
                int(focus_span_end),
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
            INSERT INTO news_event_members
                   (event_id, item_id, joined_at_ms, match_kind, jaccard_estimate, fact_id, fact_text)
            VALUES (%s, %s, %s, 'leader', NULL, %s, %s) ON CONFLICT DO NOTHING
            """,
            (event_id, leader_item_id, int(opened_at_ms), focus_fact_id, focus_fact_text),
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
        fact_id: str,
        fact_text: str,
        now_ms: int,
    ) -> bool:
        cursor = self.conn.execute(
            """
            INSERT INTO news_event_members
                   (event_id, item_id, joined_at_ms, match_kind, jaccard_estimate, fact_id, fact_text)
            VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING
            """,
            (event_id, item_id, int(joined_at_ms), match_kind, jaccard_estimate, fact_id, fact_text),
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

    def unpublished_candidates(
        self, *, older_than_ms: int, newer_than_ms: int, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Admitted Events that never left the process, inside the rescue window.

        Bounded on both sides (#76). The lower bound skips Events still mid-publish; the upper bound stops the
        catch-up from delivering something the reader can no longer use — an unbounded scan once sent a 30.6 h old
        exchange notice. Events past the ceiling stay in the table, unpublished and un-judged; they keep reading as
        pending in the feed, which is what `_OUTCOME_GROUP_SQL` and `event_outcome()` both already say. Teaching
        those two the ceiling would mean encoding one rule twice — once in SQL, once in Python — so the give-up is
        surfaced by `expired_unpublished_count()` and a Janitor warning instead.
        """

        rows = self.conn.execute(
            f"""
            SELECT event_id FROM news_events
             WHERE published_at_ms IS NULL AND admission IN ({_ADMITTED_SQL})
               AND opened_at_ms <= %s AND opened_at_ms >= %s
             ORDER BY opened_at_ms LIMIT %s
            """,
            (int(older_than_ms), int(newer_than_ms), int(limit)),
        ).fetchall()
        return [dict(r) for r in rows]

    def outbox_scan(
        self, *, older_than_ms: int, newer_than_ms: int, limit: int = 50
    ) -> tuple[list[dict[str, Any]], int]:
        """One round trip for the Janitor turn: the Events to rescue, and how many the ceiling gave up on.

        Kept to a single read on purpose — the Janitor runs every 60 s against the same pool the worker probes
        use, and a second query per turn is pure contention for a number that is almost always zero.
        """

        return (
            self.unpublished_candidates(older_than_ms=older_than_ms, newer_than_ms=newer_than_ms, limit=limit),
            self._expired_unpublished_count(older_than_ms=newer_than_ms),
        )

    def _expired_unpublished_count(self, *, older_than_ms: int) -> int:
        """Admitted Events the catch-up has given up on — surfaced so the ceiling is never silent (#76)."""

        row = self.conn.execute(
            f"""
            SELECT count(*) AS n FROM news_events
             WHERE published_at_ms IS NULL AND admission IN ({_ADMITTED_SQL})
               AND opened_at_ms < %s
            """,
            (int(older_than_ms),),
        ).fetchone()
        return int(row["n"]) if row else 0

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

    def _current_event_card(self, event_id: str) -> dict[str, Any] | None:
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

    def append_evidence_snapshot(
        self,
        *,
        event_id: str,
        now_ms: int,
        focus_item_id: str | None = None,
        focus_fact: Any | None = None,
    ) -> dict[str, Any]:
        """Append the current focused evidence when its canonical content changed.

        The hash excludes timestamps and the version counter.  Re-consuming the
        same member is therefore a zero-write, while a stronger/new member gets
        a new immutable version.  The table trigger rejects UPDATE and DELETE.
        """

        card = self._current_event_card(event_id)
        if card is None:
            raise ValueError("news_event_missing")
        members = self.conn.execute(
            """
            SELECT m.item_id, m.fact_id, m.fact_text, m.joined_at_ms, m.match_kind, m.jaccard_estimate,
                   i.reporting_origin, i.canonical_url, i.provider_metadata, i.provenance
              FROM news_event_members m
              JOIN news_items i ON i.item_id = m.item_id
             WHERE m.event_id = %s
             ORDER BY m.joined_at_ms, m.item_id, m.fact_id
            """,
            (event_id,),
        ).fetchall()
        latest = self.conn.execute(
            """
            SELECT evidence_version, evidence_sha256, focus_fact_id, snapshot, provenance, release_eligible,
                   created_at_ms
              FROM news_event_evidence_snapshots
             WHERE event_id = %s ORDER BY evidence_version DESC LIMIT 1
            """,
            (event_id,),
        ).fetchone()
        if (focus_item_id is None) != (focus_fact is None):
            raise ValueError("news_event_evidence_focus_incomplete")
        previous = dict(latest["snapshot"] or {}) if latest is not None else {}
        if focus_fact is not None:
            focus = {
                "fact_id": str(focus_fact.fact_id),
                "text": str(focus_fact.text),
                "context": str(focus_fact.context),
                "method": str(focus_fact.method),
                "span_start": int(focus_fact.span_start),
                "span_end": int(focus_fact.span_end),
            }
            source = self.conn.execute(
                """
                SELECT item_id AS leader_item_id, canonical_url AS leader_url, reporting_origin,
                       provider_metadata, provenance, published_at_ms AS leader_published_at_ms, raw_first_line
                  FROM news_items WHERE item_id = %s
                """,
                (focus_item_id,),
            ).fetchone()
            if source is None:
                raise ValueError("news_event_evidence_focus_item_missing")
            focus_source = dict(source)
        elif previous:
            focus = dict(previous.get("focus_fact") or {})
            focus_source = dict(previous.get("card") or {})
        else:
            focus = {
                "fact_id": str(card.get("focus_fact_id") or ""),
                "text": str(card.get("focus_fact_text") or card.get("leader_title") or ""),
                "context": str(card.get("focus_fact_context") or ""),
                "method": str(card.get("focus_fact_method") or "whole_item"),
                "span_start": int(card.get("focus_span_start") or 0),
                "span_end": int(card.get("focus_span_end") or 0),
            }
            focus_source = card
        snapshot_card = {
            key: card.get(key)
            for key in (
                "event_id",
                "leader_item_id",
                "family",
                "comparison_fingerprint",
                "comparison_title",
                "opened_at_ms",
                "last_member_at_ms",
                "expires_at_ms",
                "member_count",
                "admission",
                "priority",
                "provider_score_max",
                "engine_type",
                "asset_class",
                "grounded_assets",
                "watchlist_hits",
                "macro_lexicon",
                "storyline_key",
                "ingest_mode",
                "trace_id",
                "leader_url",
                "reporting_origin",
                "provider_metadata",
                "provenance",
                "leader_published_at_ms",
                "raw_first_line",
            )
        }
        snapshot_card.update(
            {
                key: focus_source.get(key)
                for key in (
                    "leader_item_id",
                    "leader_url",
                    "reporting_origin",
                    "provider_metadata",
                    "provenance",
                    "leader_published_at_ms",
                    "raw_first_line",
                )
            }
        )
        # The SemanticJudge sees one question, never the parent digest.  `raw_first_line` exists to recover a
        # subject that title normalization dropped; on a split digest `leader_title` is already the bullet's own
        # unnormalized text, and the parent's first line is a *different* bullet — it can only mislead.
        snapshot_card.update(
            {
                "leader_title": focus["text"],
                "leader_description": focus["context"],
                "focus_fact_id": focus["fact_id"],
            }
        )
        if focus.get("method") == "explicit_numbered":
            snapshot_card["raw_first_line"] = ""
        # #154: how old the source artifact already was when the provider pushed it. Derived from the same
        # parse that produces the artifact identity, so there is one owner of the rule and nothing to persist.
        _, artifact_published_at_ms = source_artifact_identity(str(snapshot_card.get("leader_url") or ""))
        pushed_at_ms = snapshot_card.get("leader_published_at_ms")
        if artifact_published_at_ms is not None and pushed_at_ms:
            snapshot_card["source_age_s"] = max(0, (int(pushed_at_ms) - artifact_published_at_ms) // 1000)
        snapshot = {
            "schema_version": "news_event_evidence_v1",
            "event_id": event_id,
            "focus_fact": focus,
            "card": snapshot_card,
            "members": [
                {
                    "item_id": str(row["item_id"]),
                    "fact_id": str(row["fact_id"]),
                    "fact_text": str(row["fact_text"]),
                    "joined_at_ms": int(row["joined_at_ms"]),
                    "match_kind": str(row["match_kind"]),
                    "jaccard_estimate": row["jaccard_estimate"],
                    "reporting_origin": str(row["reporting_origin"] or ""),
                    "canonical_url": row["canonical_url"],
                    "provider_metadata": dict(row["provider_metadata"] or {}),
                    "provenance": list(row["provenance"] or []),
                }
                for row in members
            ],
            "provenance": "observed",
        }
        serialized = _dumps(snapshot)
        evidence_sha = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        if latest is not None and str(latest["evidence_sha256"]) == evidence_sha:
            return dict(latest)
        version = int(latest["evidence_version"]) + 1 if latest is not None else 1
        row = self.conn.execute(
            """
            INSERT INTO news_event_evidence_snapshots (
              event_id, evidence_version, focus_fact_id, evidence_sha256,
              provenance, release_eligible, snapshot, created_at_ms
            ) VALUES (%s, %s, %s, %s, 'observed', true, %s::jsonb, %s)
            RETURNING evidence_version, evidence_sha256, focus_fact_id, snapshot, provenance, release_eligible,
                      created_at_ms
            """,
            (event_id, version, focus["fact_id"], evidence_sha, serialized, int(now_ms)),
        ).fetchone()
        if row is None:
            raise RuntimeError("news_event_evidence_insert_failed")
        return dict(row)

    def latest_evidence_snapshot(self, event_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT event_id, evidence_version, focus_fact_id, evidence_sha256, provenance,
                   release_eligible, snapshot, created_at_ms
              FROM news_event_evidence_snapshots
             WHERE event_id = %s ORDER BY evidence_version DESC LIMIT 1
            """,
            (event_id,),
        ).fetchone()
        return dict(row) if row else None

    def event_card(self, event_id: str) -> dict[str, Any] | None:
        """The exact latest immutable evidence card the SemanticJudge may read."""

        evidence = self.latest_evidence_snapshot(event_id)
        if evidence is None:
            return None
        snapshot = dict(evidence.get("snapshot") or {})
        card = dict(snapshot.get("card") or {})
        card.update(
            {
                "evidence_version": int(evidence["evidence_version"]),
                "evidence_sha256": str(evidence["evidence_sha256"]),
                "focus_fact_id": str(evidence["focus_fact_id"]),
                "evidence_provenance": str(evidence["provenance"]),
                "evidence_release_eligible": bool(evidence["release_eligible"]),
            }
        )
        return card

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

    # ------------------------------------------------------------------ open-interest rank ledger
    def recent_oi_signal_times(
        self, *, symbol: str, metric_version: str, since_ms: int, before_ms: int, exclude_event_id: str = ""
    ) -> list[int]:
        """Every *other* frame recorded for this symbol inside the window, newest first.

        The rank rule counts frames, not pushes: a frame the rule rejected still happened and still
        moves the next one further down the run. ``before_ms`` is the judged frame's own timestamp, so
        a frame processed out of order — the outbox rescue, or the retry lane — is not ranked behind
        frames that arrived after it. It is inclusive because the provider can stamp two frames for one
        symbol with the same publication millisecond, and the earlier one still happened;
        ``exclude_event_id`` is what keeps a redelivery out of its own history.
        """

        rows = self.conn.execute(
            "SELECT observed_at_ms FROM news_oi_signals "
            "WHERE metric_version = %s AND symbol = %s "
            "AND observed_at_ms > %s AND observed_at_ms <= %s AND event_id <> %s "
            "ORDER BY observed_at_ms DESC LIMIT 64",
            (metric_version, symbol, int(since_ms), int(before_ms), exclude_event_id),
        ).fetchall()
        return [int(row["observed_at_ms"]) for row in rows]

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

    # ------------------------------------------------------------------ public trade-candidate projections (#104)
    # Two bounded, point-in-time reads that `tracefold.trading` consumes through the composition root.
    # They return rows, not Trading models: `news -> platform` is the dependency rule, so the seam that
    # knows both packages is the only place that builds a Trading shape. Neither read joins a reaction,
    # a review or a later member — a manifest that can see the future is a backtest that proves nothing.
    def trade_candidate_oi_rows(
        self,
        *,
        metric_version: str,
        after_created_at_ms: int,
        until_created_at_ms: int,
        max_rank_in_window: int,
        min_oi_value_usd: int,
        limit: int = 64,
    ) -> list[dict[str, Any]]:
        """Deterministic telemetry verdicts that pushed, with their rank-ledger row and the frame's venue.

        `venue` comes from the Item's own `provider_metadata.source` rather than the ledger, which does
        not store it. That field is the single strongest discriminator the OI research measured
        (Hyperliquid +1.35% vs Binance -0.26% at 4 h), so a projection that dropped it would leave the
        trading lane unable to test its best-supported hypothesis.
        """

        rows = self.conn.execute(
            """
            SELECT v.event_id,
                   v.created_at_ms          AS verdict_created_at_ms,
                   v.final_decision,
                   v.program_version,
                   s.metric_version,
                   s.symbol,
                   s.direction,
                   s.oi_change_bps,
                   s.oi_value_usd,
                   s.whale_long_profit_bps,
                   s.whale_oi_ratio_bps,
                   s.rank_in_window,
                   s.observed_at_ms,
                   e.ingest_mode,
                   i.provider_metadata ->> 'source' AS venue
              FROM news_verdicts v
              JOIN news_oi_signals s
                ON s.event_id = v.event_id AND s.metric_version = %s
              JOIN news_events e ON e.event_id = v.event_id
              LEFT JOIN news_items i ON i.item_id = e.leader_item_id
             WHERE v.stage = 'triage'
               AND v.program_version = 'news_oi_signal_v1'
               AND v.final_decision IN ('push', 'escalate')
               AND v.degraded = false
               AND e.ingest_mode = 'live'
               AND v.created_at_ms > %s
               AND v.created_at_ms <= %s
               AND s.rank_in_window <= %s
               AND s.oi_value_usd >= %s
             ORDER BY v.created_at_ms, v.event_id
             LIMIT %s
            """,
            (
                metric_version,
                int(after_created_at_ms),
                int(until_created_at_ms),
                int(max_rank_in_window),
                int(min_oi_value_usd),
                int(limit),
            ),
        ).fetchall()
        return [dict(row) for row in rows]

    def trade_candidate_news_rows(
        self,
        *,
        after_created_at_ms: int,
        until_created_at_ms: int,
        limit: int = 64,
    ) -> list[dict[str, Any]]:
        """Model Triage verdicts that pushed on a crypto-class Event, frozen at the verdict cutoff.

        Only the structural conditions live in SQL. Single-primary, grounding, novelty and magnitude are
        the trading lane's own eligibility rules and stay pure functions over the verdict document, so
        they are testable without a database and cannot silently diverge from the funnel report.
        """

        rows = self.conn.execute(
            """
            SELECT v.event_id,
                   v.created_at_ms  AS verdict_created_at_ms,
                   v.final_decision,
                   v.evidence_version,
                   v.evidence_sha256,
                   v.focus_fact_id,
                   v.verdict,
                   v.program_version,
                   v.policy_version,
                   e.opened_at_ms,
                   e.comparison_fingerprint,
                   e.asset_class,
                   e.grounded_assets,
                   e.ingest_mode,
                   i.source_artifact_id,
                   i.canonical_url
              FROM news_verdicts v
              JOIN news_events e ON e.event_id = v.event_id
              LEFT JOIN news_items i ON i.item_id = e.leader_item_id
             WHERE v.stage = 'triage'
               AND v.program_version IS DISTINCT FROM 'news_oi_signal_v1'
               AND v.final_decision IN ('push', 'escalate')
               AND v.degraded = false
               AND e.ingest_mode = 'live'
               AND e.asset_class = 'crypto'
               AND v.created_at_ms > %s
               AND v.created_at_ms <= %s
             ORDER BY v.created_at_ms, v.event_id
             LIMIT %s
            """,
            (int(after_created_at_ms), int(until_created_at_ms), int(limit)),
        ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            record = dict(row)
            # #154/#157 keep the artifact id as a column and derive its publication time from the URL's
            # own identity (a snowflake, for x.com), so the projection derives it the same way the
            # delivery card's `source_age_s` does rather than inventing a second answer.
            _, published_at_ms = source_artifact_identity(str(record.pop("canonical_url", "") or ""))
            record["source_published_at_ms"] = published_at_ms
            out.append(record)
        return out

    def trade_candidate_instrument(self, *, base_symbol: str, venues: Sequence[str]) -> list[dict[str, Any]]:
        """Exactly-listed native crypto perpetuals for one underlying, in the caller's venue order.

        `instrument_class = 'crypto'` is not decoration: Binance labels its 169 TradFi perps `EQUITY`
        and friends, so a `WMT` Event whose Gate class says crypto still resolves to nothing here.
        HIP-3 builder venues (`hl.xyz`) are excluded by naming the two native perp venues explicitly.
        """

        if not venues:
            return []
        rows = self.conn.execute(
            """
            SELECT venue, venue_symbol, base_symbol, instrument_class, quote_asset, status, last_seen_ms
              FROM news_market_instruments
             WHERE base_symbol = %s
               AND venue = ANY(%s)
               AND status = 'trading'
               AND instrument_class = 'crypto'
             -- Deterministic, because the caller freezes the first row per venue into an immutable
             -- payload. `binance.perp` is snapshotted without a quote filter, so DOGEUSDT, DOGEUSDC and
             -- any dated contract all match; unspecified row order would let two identical manifests
             -- resolve to different books and break "replayable from the case row alone".
             ORDER BY venue,
                      CASE quote_asset WHEN 'USDT' THEN 0 WHEN 'USDC' THEN 1 ELSE 2 END,
                      length(venue_symbol),
                      venue_symbol
            """,
            (str(base_symbol or "").strip().upper(), list(venues)),
        ).fetchall()
        return [dict(row) for row in rows]

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
              throttled_by, verdict, model, program_version, program_sha256, degraded, error_code, trace, created_at_ms
              , evidence_version, evidence_sha256, focus_fact_id
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s::jsonb, %s,
                      %s, %s, %s)
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

    def active_canary(self) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM news_canary_activations WHERE state = 'active' ORDER BY activated_at_ms DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None

    def canary_status(self) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM news_canary_activations ORDER BY created_at_ms DESC LIMIT 1").fetchone()
        if row is None:
            return {"state": "inactive", "activation": None, "assignments": {"stable": 0, "candidate": 0}}
        activation = dict(row)
        counts = self.conn.execute(
            """
            SELECT arm, count(*) AS n
              FROM news_agent_assignments
             WHERE activation_id = %s
             GROUP BY arm
            """,
            (activation["activation_id"],),
        ).fetchall()
        return {
            "state": activation["state"],
            "activation": activation,
            "assignments": {str(item["arm"]): int(item["n"]) for item in counts},
        }

    def canary_candidate_eligible(self, candidate_manifest_sha: str) -> bool:
        row = self.conn.execute(
            """
            SELECT 1 AS ok
              FROM news_learning_artifacts
             WHERE kind = 'release_evidence'
               AND payload->>'candidate_sha' = %s
               AND payload->>'stage' = 'shadow'
               AND payload->>'gate_outcome' = 'pass'
             LIMIT 1
            """,
            (candidate_manifest_sha,),
        ).fetchone()
        return bool(row)

    def arm_canary(
        self,
        *,
        activation_id: str,
        baseline_bundle_sha: str,
        candidate_manifest_sha: str,
        candidate_bundle_sha: str,
        selector_version: str,
        exposure_bps: int,
        eligibility_profile_sha: str,
        rolling_profile_sha: str,
        now_ms: int,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO news_canary_activations(
              activation_id, baseline_bundle_sha, candidate_manifest_sha, candidate_bundle_sha,
              selector_version, exposure_bps, eligibility_profile_sha,
              rolling_profile_sha, state, revision, created_at_ms, activated_at_ms
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'active',1,%s,%s)
            """,
            (
                activation_id,
                baseline_bundle_sha,
                candidate_manifest_sha,
                candidate_bundle_sha,
                selector_version,
                int(exposure_bps),
                eligibility_profile_sha,
                rolling_profile_sha,
                int(now_ms),
                int(now_ms),
            ),
        )
        self._append_learning_artifact(
            "deployment_receipt",
            {
                "action": "canary_arm",
                "activation_id": activation_id,
                "baseline_bundle_sha": baseline_bundle_sha,
                "candidate_manifest_sha": candidate_manifest_sha,
                "candidate_bundle_sha": candidate_bundle_sha,
                "selector_version": selector_version,
                "exposure_bps": int(exposure_bps),
                "eligibility_profile_sha": eligibility_profile_sha,
                "rolling_profile_sha": rolling_profile_sha,
                "activated_at_ms": int(now_ms),
            },
            parent_sha=candidate_manifest_sha,
            created_by="canary_control",
            now_ms=now_ms,
        )

    def transition_canary(
        self,
        *,
        activation_id: str,
        target_state: str,
        reason: str,
        now_ms: int,
    ) -> bool:
        if target_state not in {"armed", "active", "tripped", "closed"}:
            raise ValueError("news_canary_transition_invalid")
        row = self.conn.execute(
            "SELECT * FROM news_canary_activations WHERE activation_id = %s FOR UPDATE",
            (activation_id,),
        ).fetchone()
        if row is None:
            raise ValueError("news_canary_activation_not_found")
        if row["state"] in {"tripped", "closed"}:
            return False
        allowed_sources = {
            "armed": {"active"},
            "active": {"armed"},
            "tripped": {"armed", "active"},
            "closed": {"armed", "active"},
        }[target_state]
        if str(row["state"]) not in allowed_sources:
            return False
        stamp_column = {
            "armed": "held_at_ms",
            "active": "resumed_at_ms",
            "tripped": "tripped_at_ms",
            "closed": "closed_at_ms",
        }[target_state]
        reason_column = "hold_reason" if target_state in {"armed", "active"} else "trip_reason"
        cursor = self.conn.execute(
            f"""
            UPDATE news_canary_activations
               SET state = %s, revision = revision + 1, {reason_column} = %s, {stamp_column} = %s
             WHERE activation_id = %s AND revision = %s AND state = %s
            """,
            (
                target_state,
                reason[:200],
                int(now_ms),
                activation_id,
                int(row["revision"]),
                row["state"],
            ),
        )
        changed = bool(cursor.rowcount)
        if changed:
            kind = "rollback_receipt" if target_state == "tripped" else "deployment_receipt"
            action = {
                "armed": "canary_hold",
                "active": "canary_resume",
                "tripped": "canary_trip",
                "closed": "canary_close",
            }[target_state]
            self._append_learning_artifact(
                kind,
                {
                    "action": action,
                    "activation_id": activation_id,
                    "baseline_bundle_sha": str(row["baseline_bundle_sha"]),
                    "candidate_manifest_sha": str(row["candidate_manifest_sha"]),
                    "candidate_bundle_sha": str(row["candidate_bundle_sha"]),
                    "reason": reason[:200],
                    "transitioned_at_ms": int(now_ms),
                    "previous_revision": int(row["revision"]),
                    "new_revision": int(row["revision"]) + 1,
                },
                parent_sha=str(row["candidate_manifest_sha"]),
                created_by="canary_control",
                now_ms=now_ms,
            )
        return changed

    def evaluate_canary_rolling_slo(self, *, activation_id: str, now_ms: int) -> dict[str, Any]:
        """Evaluate one durable, pre-registered rolling candidate SLO bucket."""

        from .canary import CANARY_ROLLING_PROFILE, CANARY_ROLLING_PROFILE_SHA

        row = self.conn.execute(
            "SELECT * FROM news_canary_activations WHERE activation_id = %s FOR UPDATE",
            (activation_id,),
        ).fetchone()
        if row is None or str(row["state"]) != "active":
            return {"evaluated": False, "reason": "activation_not_active"}
        if str(row["rolling_profile_sha"]) != CANARY_ROLLING_PROFILE_SHA:
            self.transition_canary(
                activation_id=activation_id,
                target_state="tripped",
                reason="rolling_profile_hash_mismatch",
                now_ms=now_ms,
            )
            return {"evaluated": True, "tripped": True, "reason": "rolling_profile_hash_mismatch"}
        bucket_ms = int(CANARY_ROLLING_PROFILE["evaluation_bucket_ms"])
        bucket = int(now_ms) // bucket_ms * bucket_ms
        if row["rolling_last_bucket_ms"] is not None and int(row["rolling_last_bucket_ms"]) >= bucket:
            return {"evaluated": False, "reason": "bucket_already_evaluated"}
        lower = bucket - int(CANARY_ROLLING_PROFILE["lookback_ms"])
        counts = self.conn.execute(
            """
            SELECT count(*) AS n,
                   count(*) FILTER (
                     WHERE v.present IS NULL OR v.degraded OR v.error_code IS NOT NULL
                   ) AS bad_n
              FROM news_agent_assignments a
              LEFT JOIN LATERAL (
                SELECT true AS present, x.degraded, x.error_code
                  FROM news_verdicts x
                 WHERE x.event_id = a.event_id AND x.stage = 'triage'
                 ORDER BY x.created_at_ms DESC LIMIT 1
              ) v ON true
             WHERE a.activation_id = %s AND a.arm = 'candidate'
               AND a.assigned_at_ms >= %s AND a.assigned_at_ms < %s
            """,
            (activation_id, lower, bucket),
        ).fetchone()
        n = int(counts["n"] or 0)
        bad_n = int(counts["bad_n"] or 0)
        enough = n >= int(CANARY_ROLLING_PROFILE["candidate_min_n"])
        breached = enough and bad_n / n > float(CANARY_ROLLING_PROFILE["error_or_degraded_rate_max"])
        breach_windows = int(row["rolling_breach_windows"] or 0) + 1 if breached else 0
        self.conn.execute(
            """
            UPDATE news_canary_activations
               SET rolling_last_bucket_ms = %s, rolling_breach_windows = %s,
                   revision = revision + 1
             WHERE activation_id = %s AND revision = %s AND state = 'active'
            """,
            (bucket, breach_windows, activation_id, int(row["revision"])),
        )
        tripped = breach_windows >= int(CANARY_ROLLING_PROFILE["consecutive_breach_buckets"])
        if tripped:
            self.transition_canary(
                activation_id=activation_id,
                target_state="tripped",
                reason="candidate_rolling_error_slo_trip",
                now_ms=now_ms,
            )
        return {
            "evaluated": True,
            "bucket_ms": bucket,
            "candidate_n": n,
            "bad_n": bad_n,
            "breached": breached,
            "breach_windows": breach_windows,
            "tripped": tripped,
        }

    def assign_agent_arm(
        self,
        *,
        event_id: str,
        stable_bundle_sha: str,
        admission: str,
        priority: str,
        ingest_mode: str,
        now_ms: int,
    ) -> dict[str, Any]:
        existing = self.conn.execute("SELECT * FROM news_agent_assignments WHERE event_id = %s", (event_id,)).fetchone()
        if existing:
            return dict(existing)
        activation = self.active_canary()
        if activation is None:
            selection = {
                "activation_id": None,
                "arm": "stable",
                "bundle_sha": stable_bundle_sha,
                "selector_version": "stable_only_v1",
                "eligibility_reason": "no_active_canary",
            }
        elif str(activation["baseline_bundle_sha"]) != stable_bundle_sha:
            self.transition_canary(
                activation_id=str(activation["activation_id"]),
                target_state="tripped",
                reason="baseline_bundle_mismatch",
                now_ms=now_ms,
            )
            selection = {
                "activation_id": str(activation["activation_id"]),
                "arm": "stable",
                "bundle_sha": stable_bundle_sha,
                "selector_version": str(activation["selector_version"]),
                "eligibility_reason": "activation_tripped_baseline_mismatch",
            }
        else:
            from .canary import select_canary_arm

            selected = select_canary_arm(
                event_id=event_id,
                activation_id=str(activation["activation_id"]),
                baseline_bundle_sha=stable_bundle_sha,
                candidate_bundle_sha=str(activation["candidate_bundle_sha"]),
                exposure_bps=int(activation["exposure_bps"]),
                admission=admission,
                priority=priority,
                ingest_mode=ingest_mode,
            )
            selection = {
                "activation_id": str(activation["activation_id"]),
                "arm": selected.arm,
                "bundle_sha": selected.bundle_sha,
                "selector_version": str(activation["selector_version"]),
                "eligibility_reason": selected.eligibility_reason,
            }
        self.conn.execute(
            """
            INSERT INTO news_agent_assignments(
              event_id, activation_id, arm, bundle_sha, selector_version,
              eligibility_reason, assigned_at_ms
            ) VALUES (%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (event_id) DO NOTHING
            """,
            (
                event_id,
                selection["activation_id"],
                selection["arm"],
                selection["bundle_sha"],
                selection["selector_version"],
                selection["eligibility_reason"],
                int(now_ms),
            ),
        )
        row = self.conn.execute("SELECT * FROM news_agent_assignments WHERE event_id = %s", (event_id,)).fetchone()
        if row is None:
            raise RuntimeError("news_agent_assignment_insert_failed")
        return dict(row)

    def register_agent_runtime_manifest(
        self,
        *,
        manifest_sha: str,
        stable_bundle_sha: str,
        candidate_shas: Sequence[str],
        image_digest: str,
        runtime_revision: str,
        now_ms: int,
    ) -> None:
        previous_active = self.conn.execute(
            "SELECT artifact_sha, payload FROM news_learning_artifacts "
            "WHERE kind = 'active_agent' ORDER BY created_at_ms DESC LIMIT 1"
        ).fetchone()
        self.conn.execute(
            """
            INSERT INTO news_agent_runtime_manifests(
              manifest_sha, stable_bundle_sha, candidate_shas, image_digest,
              runtime_revision, registered_at_ms
            ) VALUES (%s,%s,%s::jsonb,%s,%s,%s)
            ON CONFLICT (manifest_sha) DO NOTHING
            """,
            (
                manifest_sha,
                stable_bundle_sha,
                _dumps(sorted(set(candidate_shas))),
                image_digest,
                runtime_revision,
                int(now_ms),
            ),
        )
        previous_payload = dict(previous_active["payload"] or {}) if previous_active else {}
        if previous_payload.get("runtime_manifest_sha") == manifest_sha:
            return
        active_sha = self._append_learning_artifact(
            "active_agent",
            {
                "stable_sha": stable_bundle_sha,
                "runtime_manifest_sha": manifest_sha,
                "candidate_shas": sorted(set(candidate_shas)),
                "image_digest": image_digest,
                "runtime_revision": runtime_revision,
                "registered_at_ms": int(now_ms),
            },
            parent_sha=str(previous_active["artifact_sha"]) if previous_active else None,
            created_by="worker_startup",
            now_ms=now_ms,
        )
        previous_stable = str(previous_payload["stable_sha"]) if previous_payload else None
        previous_image = str(previous_payload["image_digest"]) if previous_payload else None
        self._append_learning_artifact(
            "deployment_receipt",
            {
                "action": "runtime_deploy",
                "active_agent_sha": active_sha,
                "stable_sha": stable_bundle_sha,
                "image_digest": image_digest,
                "runtime_revision": runtime_revision,
                "previous_stable_sha": previous_stable,
                "previous_image_digest": previous_image,
                "deployed_at_ms": int(now_ms),
                "rollback_available_until_ms": int(now_ms) + 24 * 3_600_000,
            },
            parent_sha=active_sha,
            created_by="worker_startup",
            now_ms=now_ms,
        )

    def _append_learning_artifact(
        self,
        kind: str,
        payload: Mapping[str, Any],
        *,
        parent_sha: str | None,
        created_by: str,
        now_ms: int,
    ) -> str:
        public = dict(payload)
        artifact_sha = hashlib.sha256(_dumps({"kind": kind, "payload": public}).encode("utf-8")).hexdigest()
        self.conn.execute(
            """
            INSERT INTO news_learning_artifacts(
              artifact_sha, kind, parent_sha, payload, created_by, created_at_ms
            ) VALUES (%s,%s,%s,%s::jsonb,%s,%s)
            ON CONFLICT (artifact_sha) DO NOTHING
            """,
            (artifact_sha, kind, parent_sha, _dumps(public), created_by, int(now_ms)),
        )
        return artifact_sha

    # ------------------------------------------------------------------ janitor / retention
    def expire_bands(self, *, now_ms: int) -> int:
        cursor = self.conn.execute("DELETE FROM news_event_bands WHERE expires_at_ms < %s", (int(now_ms),))
        return int(cursor.rowcount or 0)

    def purge_learning_retention(self, *, batch_size: int = 500) -> dict[str, Any]:
        """Run the database-owned bounded learning-evidence retention policy."""

        row = self.conn.execute(
            "SELECT purge_news_learning_retention(%s) AS result",
            (int(batch_size),),
        ).fetchone()
        return dict(row["result"] or {})

    def record_learning_retention_error(self, *, error_code: str, now_ms: int) -> None:
        self.conn.execute(
            """
            UPDATE news_learning_retention_state
               SET last_error_code = %s, updated_at_ms = %s
             WHERE singleton
            """,
            (str(error_code)[:200], int(now_ms)),
        )

    def purge_before(self, *, cutoff_ms: int, judged_cutoff_ms: int | None = None) -> int:
        """Delete raw Items older than ``cutoff_ms``, keeping any Item that is evidence for a judged or reviewed
        Event newer than ``judged_cutoff_ms`` (#81).

        Deleting `news_items` cascades to `news_events` (leader FK) and from there to verdicts, deliveries,
        members, assets, bands, snapshots and reviews, so one retention number decides the lifetime of the
        whole learning plane. An Item is evidence when *any* Event it belongs to — as leader or as a later
        member, which is what a rebuild of the Triage input needs — carries a verdict or review. Passing no
        ``judged_cutoff_ms`` keeps the old one-tier behaviour for callers that do not care.
        """

        if judged_cutoff_ms is None:
            cursor = self.conn.execute("DELETE FROM news_items WHERE observed_at_ms < %s", (int(cutoff_ms),))
            return int(cursor.rowcount or 0)
        cursor = self.conn.execute(
            """
            DELETE FROM news_items i
             WHERE i.observed_at_ms < %s
               AND NOT EXISTS (
                     SELECT 1
                       FROM news_event_members m
                       JOIN news_events e ON e.event_id = m.event_id
                      WHERE m.item_id = i.item_id
                        AND e.opened_at_ms >= %s
                        AND (EXISTS (SELECT 1 FROM news_verdicts v WHERE v.event_id = e.event_id)
                          OR EXISTS (SELECT 1 FROM news_reviews r WHERE r.event_id = e.event_id)
                          OR EXISTS (SELECT 1 FROM news_learning_cases c WHERE c.event_id = e.event_id))
                   )
               AND NOT EXISTS (
                     SELECT 1
                       FROM news_events e2
                      WHERE e2.leader_item_id = i.item_id
                        AND e2.opened_at_ms >= %s
                        AND (EXISTS (SELECT 1 FROM news_verdicts v WHERE v.event_id = e2.event_id)
                          OR EXISTS (SELECT 1 FROM news_reviews r WHERE r.event_id = e2.event_id)
                          OR EXISTS (SELECT 1 FROM news_learning_cases c WHERE c.event_id = e2.event_id))
                   )
            """,
            (int(cutoff_ms), int(judged_cutoff_ms), int(judged_cutoff_ms)),
        )
        return int(cursor.rowcount or 0)

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
        # `where` / `params` accumulate the predicates every outcome group shares — the window and the reader's
        # filters. The outcome group itself and the cursor are appended after `_feed_counts` has taken a copy,
        # so the tab counts describe the whole filtered set rather than the page being served.
        params: list[Any] = []
        where = ["e.ingest_mode IN ('live', 'recovery')"]
        window_hours = int(hours) if hours else None
        if window_hours:
            # The response echoes `hours`, never the wall-clock bound, so an unchanged page keeps its ETag.
            since_ms = int(now_ms if now_ms is not None else time.time() * 1000) - window_hours * 3600_000
            where.append("e.opened_at_ms >= %s")
            params.append(since_ms)
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
        # Counting is worth one extra aggregate only on the first page; later pages reuse what it returned.
        # Snapshot the clauses so the outcome group and cursor appended below cannot reach the count query.
        counts = self._feed_counts(where=list(where), params=list(params)) if cursor_opened is None else None
        if outcome in _OUTCOME_GROUP_SQL:
            where.append(_OUTCOME_GROUP_SQL[outcome])
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
                   t.model_decision, t.verdict AS triage_verdict,
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
            "counts": counts,
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

    def _feed_counts(self, *, where: list[str], params: list[Any]) -> dict[str, int]:
        """How the reader's current filter splits across the three outcome groups.

        The three predicates partition the feed exactly (see `_OUTCOME_GROUP_SQL`), so one pass with FILTER
        aggregates answers all four tabs. The joins mirror the feed query so a row counts here if and only if
        it would be served there, but the lateral takes only the column the predicates read rather than the
        whole verdict row.

        This is an unbounded aggregate on a three-second poll: it costs one pass over the filtered set, which
        is the last 24 h by default but the whole retention when the reader picks `hours=all`. Measured at
        19 ms over the entire table at ~2k Events / 1.3 days of retention; re-measure before letting either
        grow much, and cap the window here if it stops being free.
        """
        row = self.conn.execute(
            f"""
            SELECT count(*) AS total,
                   count(*) FILTER (WHERE {_OUTCOME_GROUP_SQL["pushed"]}) AS pushed,
                   count(*) FILTER (WHERE {_OUTCOME_GROUP_SQL["held"]}) AS held,
                   count(*) FILTER (WHERE {_OUTCOME_GROUP_SQL["pending"]}) AS pending
              FROM news_events e
              JOIN news_items i ON i.item_id = e.leader_item_id
              LEFT JOIN LATERAL (
                SELECT v.final_decision FROM news_verdicts v
                 WHERE v.event_id = e.event_id AND v.stage = 'triage'
                 ORDER BY v.created_at_ms DESC LIMIT 1
              ) t ON true
              LEFT JOIN news_deliveries d ON d.event_id = e.event_id AND d.kind = 'first'
             WHERE {" AND ".join(where)}
            """,
            tuple(params),
        ).fetchone()
        return {key: int((row or {}).get(key) or 0) for key in ("total", "pushed", "held", "pending")}

    def event_detail(self, event_id: str) -> dict[str, Any] | None:
        card = self._current_event_card(event_id)
        if card is None:
            return None
        members = self.conn.execute(
            """
            SELECT m.item_id, m.joined_at_ms, m.match_kind, m.jaccard_estimate, i.title, i.canonical_url,
                   i.reporting_origin, i.published_at_ms, i.provenance, i.description, m.fact_id, m.fact_text
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
                "fact_id": r["fact_id"],
                "fact_text": r["fact_text"],
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
                "card": dict(r["card"] or {}),
                "receipt": r["receipt"],
            }
            for r in deliveries
        ]
        outcome, timeline = event_timeline(
            event=event, members=member_rows, verdicts=verdict_rows, deliveries=delivery_rows
        )
        latest_triage = next((v for v in reversed(verdict_rows) if v.get("stage") == "triage"), None)
        return {
            "event": event,
            "outcome": outcome.as_dict(),
            "triage": _triage_summary(
                final_decision=(latest_triage or {}).get("final_decision"),
                override_rule=(latest_triage or {}).get("override_rule"),
                throttled_by=(latest_triage or {}).get("throttled_by"),
                degraded=(latest_triage or {}).get("degraded"),
                error_code=(latest_triage or {}).get("error_code"),
                model_decision=(latest_triage or {}).get("model_decision"),
                verdict=(latest_triage or {}).get("verdict") or {},
                full=True,
            ),
            "timeline": timeline,
            "members": member_rows,
            "verdicts": verdict_rows,
            "deliveries": delivery_rows,
            "review": self._review_summary(event_id),
            "evidence_snapshots": [
                {
                    "event_id": row["event_id"],
                    "evidence_version": int(row["evidence_version"]),
                    "focus_fact_id": row["focus_fact_id"],
                    "evidence_sha256": row["evidence_sha256"],
                    "provenance": row["provenance"],
                    "release_eligible": bool(row["release_eligible"]),
                    "snapshot": dict(row["snapshot"] or {}),
                    "created_at_ms": int(row["created_at_ms"]),
                }
                for row in self.conn.execute(
                    "SELECT * FROM news_event_evidence_snapshots WHERE event_id = %s ORDER BY evidence_version",
                    (event_id,),
                ).fetchall()
            ],
            "reader_receipt": ReaderReceipt.from_delivery(delivery_rows[-1] if delivery_rows else None).model_dump(
                mode="json"
            ),
        }

    def _review_summary(self, event_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            """
            SELECT j.*, counts.judgment_n
              FROM (
                SELECT count(*) AS judgment_n FROM news_reviews
                 WHERE event_id = %s AND review_kind = 'judgment'
              ) counts
              LEFT JOIN LATERAL (
                SELECT judgment.*
                  FROM news_reviews acceptance
                  JOIN news_reviews judgment ON judgment.review_id = acceptance.accepts_review_id
                 WHERE acceptance.review_kind = 'acceptance' AND judgment.event_id = %s
                 ORDER BY acceptance.created_at_ms DESC, acceptance.review_id DESC LIMIT 1
              ) j ON true
            """,
            (event_id, event_id),
        ).fetchone()
        if row is None:
            return {"judgment_n": 0, "accepted": None, "uncertain": False}
        accepted = None
        if row.get("review_id"):
            accepted = {
                "review_id": row["review_id"],
                "should_push": row["should_push"],
                "dimensions": dict(row["dimensions"] or {}),
                "novelty": dict(row["novelty"] or {}),
                "first_bad_owner": row["first_bad_owner"],
                "evidence_refs": list(row["evidence_refs"] or []),
                "expected_correction": row["expected_correction"],
                "note": row["note"],
                "reviewer": row["reviewer"],
                "created_at_ms": int(row["created_at_ms"]),
                "rubric_version": row["rubric_version"],
                "reader_contract_version": row["reader_contract_version"],
            }
        return {
            "judgment_n": int(row["judgment_n"] or 0),
            "accepted": accepted,
            "uncertain": bool(accepted and accepted["should_push"] == "uncertain"),
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
              -- Two denominators on purpose. The funnel is the reader's view — 收到 ⊇ 送审 ⊇ 模型判断
              -- ⊇ 决定推送 ⊇ 已送达, subtracted band by band by the console — and a telemetry judgment
              -- is a judgment and its push is a card the reader received, so both count here or the
              -- containment breaks at one end or the other. Model health is a different question and
              -- gets its own denominator below: ~190 arithmetic judgments a day, never degraded,
              -- would otherwise dilute the degraded share and make the model look healthier than it is.
              (SELECT count(*) FROM news_verdicts WHERE stage = 'triage' AND created_at_ms >= %s) AS triage_24h,
              (SELECT count(*) FROM news_verdicts
                WHERE stage = 'triage' AND created_at_ms >= %s
                  AND program_version IS DISTINCT FROM 'news_oi_signal_v1') AS model_triage_24h,
              (SELECT count(*) FROM news_verdicts
                WHERE stage = 'triage' AND degraded AND created_at_ms >= %s) AS triage_degraded_24h,
              (SELECT count(*) FROM news_verdicts
                WHERE stage = 'triage' AND final_decision IN ('push','escalate')
                  AND created_at_ms >= %s) AS decided_push_24h,
              (SELECT count(*) FROM news_verdicts
                WHERE stage = 'triage' AND final_decision IN ('push','escalate')
                  AND created_at_ms >= %s
                  AND program_version = 'news_oi_signal_v1') AS telemetry_push_24h,
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
            (hour_ago, *([day_ago] * 13)),
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
        funnel = self._funnel_24h(day_ago=day_ago)
        retention = self.conn.execute("SELECT * FROM news_learning_retention_state WHERE singleton").fetchone()
        return {
            "ingest": {
                "connected": bool(ingest["connected"]) if ingest else False,
                "last_frame_at_ms": ingest["last_frame_at_ms"] if ingest else None,
                "last_publish_at_ms": ingest["last_publish_at_ms"] if ingest else None,
                "last_error_code": ingest["last_error_code"] if ingest else None,
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
            "learning_retention": {
                "last_run_at_ms": retention["last_run_at_ms"] if retention else None,
                "eligible_recordings": int(retention["eligible_recordings"] or 0) if retention else 0,
                "eligible_cases": int(retention["eligible_cases"] or 0) if retention else 0,
                "eligible_artifacts": int(retention["eligible_artifacts"] or 0) if retention else 0,
                "deleted_recordings": int(retention["deleted_recordings"] or 0) if retention else 0,
                "deleted_cases": int(retention["deleted_cases"] or 0) if retention else 0,
                "deleted_artifacts": int(retention["deleted_artifacts"] or 0) if retention else 0,
                "oldest_recording_age_ms": retention["oldest_recording_age_ms"] if retention else None,
                "oldest_case_age_ms": retention["oldest_case_age_ms"] if retention else None,
                "oldest_artifact_age_ms": retention["oldest_artifact_age_ms"] if retention else None,
                "last_error_code": retention["last_error_code"] if retention else None,
                "updated_at_ms": int(retention["updated_at_ms"]) if retention else None,
            },
        }

    def asset_usage_24h(self, *, now_ms: int) -> dict[str, list[str]]:
        """event_id -> the coin tags the Gate grounded it on, for the last 24 h (#87).

        The console's «符号落表» funnel segment and the «符号未落标的表» reason group both need to know which
        Events named something that exists on a venue. That answer spans two owners — this table and the #75
        instrument universe — so this half returns only its own rows and `grounding_rollup` folds them against
        `InstrumentsRepository.asset_refs`. Neither repository reaches into the other's tables.

        Only Events that carry at least one tag come back; an Event absent from the map grounded on nothing.
        At ~1.5 k Events / day and about one tag each that is a low four-figure row count beside the
        percentile aggregates `status_snapshot` already runs over the same window.
        """

        rows = self.conn.execute(
            """
            SELECT event_id, array_agg(symbol ORDER BY symbol) AS symbols
              FROM news_event_assets
             WHERE opened_at_ms >= %s
             GROUP BY event_id
            """,
            (int(now_ms) - 24 * 3600_000,),
        ).fetchall()
        return {str(row["event_id"]): [str(s) for s in (row["symbols"] or [])] for row in rows}

    def _funnel_24h(self, *, day_ago: int) -> dict[str, Any]:
        """Where the last 24 h of Events went, by named reason: Gate admissions, decide() rules, storyline keys."""

        suppressed = self.conn.execute(
            f"""
            SELECT admission, count(*) AS n FROM news_events
             WHERE opened_at_ms >= %s AND admission NOT IN ({_ADMITTED_SQL})
             GROUP BY admission ORDER BY n DESC
            """,
            (day_ago,),
        ).fetchall()
        # One pass over the last 24 h of Triage verdicts; the four named maps are folded from it in Python.
        verdict_groups = self.conn.execute(
            """
            SELECT final_decision, COALESCE(override_rule, 'unknown') AS rule,
                   COALESCE(throttled_by, 'unknown') AS key, degraded, COALESCE(error_code, 'unknown') AS code,
                   COALESCE(trace ->> 'seen_scope', '') AS seen_scope,
                   count(*) AS n
              FROM news_verdicts
             WHERE stage = 'triage' AND created_at_ms >= %s
             GROUP BY 1, 2, 3, 4, 5, 6
            """,
            (day_ago,),
        ).fetchall()
        dropped: dict[str, int] = {}
        throttled: dict[str, int] = {}
        pushed_by_rule: dict[str, int] = {}
        degraded_by_code: dict[str, int] = {}
        # Duplicate withholds by measurement path. Policy v7 writes `all` for
        # every ordinary push comparison; `throttled` is retained for old rows.
        duplicates: dict[str, int] = {"throttled": 0, "all": 0}
        for row in verdict_groups:
            n = int(row["n"])
            final = str(row["final_decision"])
            if final == "drop":
                dropped[str(row["rule"])] = dropped.get(str(row["rule"]), 0) + n
            elif final == "throttled":
                throttled[str(row["key"])] = throttled.get(str(row["key"]), 0) + n
                if str(row["key"]).endswith(":seen"):
                    # Old rows without `seen_scope` came from the pre-v7
                    # count-throttle path; keep them in the historical bucket.
                    scope = str(row["seen_scope"] or "") or "throttled"
                    duplicates[scope] = duplicates.get(scope, 0) + n
            elif final in {"push", "escalate"}:
                pushed_by_rule[str(row["rule"])] = pushed_by_rule.get(str(row["rule"]), 0) + n
            if row["degraded"]:
                degraded_by_code[str(row["code"])] = degraded_by_code.get(str(row["code"]), 0) + n
        # Both Review v2 shapes of "the reader should have got this": an accepted Event judgment and an
        # accepted ExternalMissSnapshot.  The latter is the only observed upper bound on upstream recall.
        missed = self.conn.execute(
            """
            SELECT count(*) FILTER (WHERE j.should_push IN ('must_push', 'should_push')) AS n,
                   count(*) FILTER (
                     WHERE j.subject_kind = 'external_miss'
                       AND j.should_push IN ('must_push', 'should_push')
                   ) AS external
              FROM news_reviews acceptance
              JOIN news_reviews j ON j.review_id = acceptance.accepts_review_id
             WHERE acceptance.review_kind = 'acceptance' AND acceptance.created_at_ms >= %s
            """,
            (day_ago,),
        ).fetchone()
        totals = self.conn.execute(
            f"""
            SELECT count(*) AS events,
                   count(*) FILTER (WHERE admission IN ({_ADMITTED_SQL})) AS admitted
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
            "reviewed_should_push_24h": int(missed["n"] or 0) if missed else 0,
            "reviewed_external_miss_24h": int(missed["external"] or 0) if missed else 0,
            "duplicates_withheld_24h": duplicates,
            "candidate_share_24h": round(admitted / events, 4) if events else None,
        }


# Feed task tabs (mirrors OUTCOME_GROUP in outcome.py, expressed over the feed's joined rows;
# tests/integration/test_news_v3_pipeline.py asserts the three predicates partition the feed exactly like
# event_outcome().group over the fixture corpus):
# pushed = the first card was sent; pending = still moving (not yet triaged, or decided push and not yet settled);
# held = everything that stopped short of a sent card (gate, drop, throttle, fallback drop, delivery failure).
_PENDING_CORE_SQL: Final = (
    f"e.admission IN ({_ADMITTED_SQL})"
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
        "focus_fact_id": card.get("focus_fact_id", ""),
        "focus_fact_text": card.get("focus_fact_text", ""),
        "focus_fact_context": card.get("focus_fact_context", ""),
        "focus_fact_method": card.get("focus_fact_method", ""),
        "focus_span_start": int(card.get("focus_span_start") or 0),
        "focus_span_end": int(card.get("focus_span_end") or 0),
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


def _triage_summary(
    *,
    final_decision: Any,
    override_rule: Any = None,
    throttled_by: Any = None,
    degraded: Any = False,
    error_code: Any = None,
    model_decision: Any = None,
    verdict: Mapping[str, Any] | None = None,
    full: bool = False,
) -> dict[str, Any] | None:
    """The reader-facing Triage summary shared by the feed row and the Event detail.

    Every business word is resolved to Chinese here so no browser owns a vocabulary table (the Feishu card in
    ``delivery.py`` emits the same `DIRECTION_ZH`/`MAGNITUDE_ZH` words, and one definition keeps the card and
    the console from drifting); the raw enum ships beside it purely so the UI can pick a visual tone.

    ``full`` is the Event detail. The feed row renders only direction/magnitude/type over 25 rows, so it takes
    the slim shape — carrying the detail fields there cost 20.7% of the feed payload for nothing."""

    if not final_decision:
        return None
    v: Mapping[str, Any] = verdict or {}
    direction = v.get("direction")
    magnitude = _optional_int(v.get("magnitude"))
    event_type = v.get("event_type")
    scope = v.get("scope")
    summary = {
        "final_decision": final_decision,
        "override_rule": override_rule,
        "throttled_by": throttled_by,
        "degraded": bool(degraded),
        "error_code": error_code,
        "direction": direction,
        "magnitude": magnitude,
        "event_type": event_type,
        "headline_zh": v.get("headline_zh"),
        "title_zh": display_title(v),
        "direction_zh": direction_zh(direction),
        "magnitude_zh": magnitude_zh(magnitude),
        "event_type_zh": event_type_zh(event_type),
    }
    if not full:
        return summary
    novelty = v.get("novelty")
    audience = v.get("audience")
    return summary | {
        "scope": scope,
        "novelty": novelty,
        "audience": audience,
        "confidence": _optional_float(v.get("confidence")),
        "actionable": _optional_bool(v.get("actionable")),
        "model_decision": model_decision,
        "why_zh": v.get("why_zh"),
        "assets": _triage_assets(v.get("assets")),
        "scope_zh": scope_zh(scope),
        "novelty_zh": novelty_zh(novelty),
        "audience_zh": audience_zh(audience),
        "decision_zh": decision_zh(final_decision),
        "model_decision_zh": decision_zh(model_decision),
    }


def _triage_assets(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    out: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        symbol = str(item.get("symbol") or "").strip()
        if not symbol:
            continue
        out.append({"symbol": symbol, "role": str(item.get("role") or "mentioned")})
    return out


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _optional_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _optional_bool(value: Any) -> bool | None:
    return bool(value) if isinstance(value, bool) else None


def _feed_row(row: Mapping[str, Any]) -> dict[str, Any]:
    triage = _triage_summary(
        final_decision=row.get("final_decision"),
        override_rule=row.get("override_rule"),
        throttled_by=row.get("throttled_by"),
        degraded=row.get("triage_degraded"),
        error_code=row.get("triage_error_code"),
        model_decision=row.get("model_decision"),
        verdict=row.get("triage_verdict") or {},
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
        "title_zh": display_title(row) or None,
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
        "program_version": row.get("program_version"),
        "program_sha256": row.get("program_sha256"),
        # Prompt identity is historical audit data. New Program verdicts leave
        # the legacy column null and never execute it.
        "prompt_version": row.get("prompt_version"),
        "degraded": bool(row.get("degraded")),
        "error_code": row.get("error_code"),
        "trace": dict(row.get("trace") or {}),
        "evidence_version": row.get("evidence_version"),
        "evidence_sha256": row.get("evidence_sha256"),
        "focus_fact_id": row.get("focus_fact_id"),
        "published_at_ms": row.get("published_at_ms"),
        "created_at_ms": int(row["created_at_ms"]),
    }


__all__ = ["NewsRepository"]
