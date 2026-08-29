"""Material News Items, Events, memberships, outbox state, and evidence snapshots."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any, cast

from ..models import ADMITTED_ADMISSIONS
from ..opennews import source_artifact_identity
from ..source_contracts import EventKind, SourceContractReason
from .sql_values import _ADMITTED_SQL, _dumps


class EventStorage:
    conn: Any

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
              provider_metadata = jsonb_set(
                news_items.provider_metadata,
                '{strategies}',
                (
                  SELECT COALESCE(
                    jsonb_agg(value ORDER BY source_rank, existing_ordinal NULLS LAST, value),
                    '[]'::jsonb
                  )
                    FROM (
                      SELECT value, min(source_rank) AS source_rank,
                             min(original_ordinal) FILTER (WHERE source_rank = 0) AS existing_ordinal
                        FROM (
                          SELECT value, 0::smallint AS source_rank, ordinality::bigint AS original_ordinal
                            FROM jsonb_array_elements(
                              COALESCE(news_items.provider_metadata -> 'strategies', '[]'::jsonb)
                            ) WITH ORDINALITY AS existing(value, ordinality)
                          UNION ALL
                          SELECT value, 1::smallint, NULL::bigint
                            FROM jsonb_array_elements(
                              COALESCE(EXCLUDED.provider_metadata -> 'strategies', '[]'::jsonb)
                            ) AS incoming(value)
                        ) combined
                       GROUP BY value
                    ) deduplicated
                ),
                true
              ),
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

    def find_artifact_event(
        self,
        *,
        source_artifact_id: str,
        dedupe_family: str,
        event_kind: EventKind,
        fingerprint: str,
        item_id: str,
        opened_after_ms: int,
        source_contract_reason: SourceContractReason | None = None,
    ) -> dict[str, Any] | None:
        """The Event another Item built from this same source artifact and this same fact (#154).

        The fingerprint is part of the key, not decoration. Without it a digest split into four FactUnits would
        collapse into one Event the second time the provider sent it, because all four units share the artifact.
        With it, unit *k* can only join unit *k*.

        What the artifact id buys is the right to ignore the two guards the text-derived path needs: the
        three-token `shareable` floor (a tweet titled `What a coincidence!` scores below it and so was never
        looked up at all — the provider sent it twice, four seconds apart, under two URL spellings, and the
        reader got two cards) and the 12 h dedupe-family window (`opened_after_ms` is the caller's longer horizon).
        Both guards exist because *text* similarity is evidence; artifact identity is not evidence, it is the
        platform's own primary key.
        """

        if not source_artifact_id:
            return None
        # Only an admitted Event carrying the v3 evidence contract may absorb a live frame. That exact
        # contract boundary prevents a post-cut Item from disappearing into pre-cut evidence without shortening
        # #154's deliberate seven-day artifact window back to the ordinary 12 h dedupe-family window. Recovery and
        # suppressed Events are excluded by admission because neither can produce the live reader card this Item
        # represents.
        row = self.conn.execute(
            """
            SELECT e.event_id, e.opened_at_ms, e.expires_at_ms, e.admission, e.published_at_ms,
                   e.source_contract_reason
              FROM news_items i
              JOIN news_event_members m ON m.item_id = i.item_id
              JOIN news_events e ON e.event_id = m.event_id
             WHERE i.source_artifact_id = %s AND i.item_id <> %s
               AND e.dedupe_family = %s AND e.event_kind = %s AND e.comparison_fingerprint = %s
               AND (
                 e.source_contract_reason IS NOT DISTINCT FROM %s
                 OR e.source_contract_reason = 'source_contract_unverified'
               )
               AND e.opened_at_ms >= %s
               AND (
                 SELECT s.provenance = 'observed'
                    AND s.snapshot ->> 'schema_version' = 'news_event_evidence_v3'
                   FROM news_event_evidence_snapshots s
                  WHERE s.event_id = e.event_id
                  ORDER BY s.evidence_version DESC LIMIT 1
               )
               AND e.admission = ANY(%s)
             ORDER BY (e.source_contract_reason IS NOT DISTINCT FROM %s) DESC,
                      e.opened_at_ms ASC LIMIT 1
            """,
            (
                source_artifact_id,
                item_id,
                dedupe_family,
                event_kind,
                fingerprint,
                source_contract_reason,
                int(opened_after_ms),
                sorted(ADMITTED_ADMISSIONS),
                source_contract_reason,
            ),
        ).fetchone()
        return dict(row) if row else None

    def find_exact_event(
        self,
        *,
        dedupe_family: str,
        event_kind: EventKind,
        fingerprint: str,
        now_ms: int,
        source_contract_reason: SourceContractReason | None = None,
    ) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT e.event_id, e.opened_at_ms, e.expires_at_ms, e.admission, e.published_at_ms,
                   e.source_contract_reason
              FROM news_events e
             WHERE e.dedupe_family = %s AND e.event_kind = %s
               AND (
                 source_contract_reason IS NOT DISTINCT FROM %s
                 OR source_contract_reason = 'source_contract_unverified'
               )
               AND e.comparison_fingerprint = %s AND e.expires_at_ms > %s
               AND (
                 SELECT s.provenance = 'observed'
                    AND s.snapshot ->> 'schema_version' = 'news_event_evidence_v3'
                   FROM news_event_evidence_snapshots s
                  WHERE s.event_id = e.event_id
                  ORDER BY s.evidence_version DESC LIMIT 1
               )
             ORDER BY (source_contract_reason IS NOT DISTINCT FROM %s) DESC,
                      opened_at_ms ASC LIMIT 1
            """,
            (dedupe_family, event_kind, source_contract_reason, fingerprint, int(now_ms), source_contract_reason),
        ).fetchone()
        return dict(row) if row else None

    def find_band_candidates(
        self,
        *,
        dedupe_family: str,
        event_kind: EventKind,
        band_keys: Sequence[str],
        now_ms: int,
        source_contract_reason: SourceContractReason | None = None,
    ) -> list[dict[str, Any]]:
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
               WHERE b.dedupe_family = %s AND b.expires_at_ms > %s
            )
            SELECT e.event_id, e.comparison_title, e.leader_title, e.opened_at_ms, e.grounded_assets
              FROM news_events e JOIN hits ON hits.event_id = e.event_id
             WHERE e.event_kind = %s
               AND e.source_contract_reason IS NOT DISTINCT FROM %s
               AND (
                 SELECT s.provenance = 'observed'
                    AND s.snapshot ->> 'schema_version' = 'news_event_evidence_v3'
                   FROM news_event_evidence_snapshots s
                  WHERE s.event_id = e.event_id
                  ORDER BY s.evidence_version DESC LIMIT 1
               )
             ORDER BY e.opened_at_ms ASC
             LIMIT 25
            """,
            (
                [p[0] for p in pairs],
                [p[1] for p in pairs],
                dedupe_family,
                int(now_ms),
                event_kind,
                source_contract_reason,
            ),
        ).fetchall()
        return [dict(r) for r in rows]

    def insert_event(
        self,
        *,
        event_id: str,
        leader_item_id: str,
        dedupe_family: str,
        event_kind: EventKind,
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
        queue_priority: str,
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
        source_contract_reason: SourceContractReason | None = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO news_events (
              event_id, leader_item_id, dedupe_family, event_kind, source_contract_reason,
              comparison_fingerprint, comparison_title, leader_title,
              focus_fact_id, focus_fact_text, focus_fact_context, focus_fact_method, focus_span_start, focus_span_end,
              opened_at_ms, last_member_at_ms, expires_at_ms, member_count, admission, queue_priority,
              provider_score_max, engine_type, asset_class, grounded_assets, watchlist_hits, macro_lexicon,
              storyline_key, context_line, ingest_mode, trace_id, created_at_ms, updated_at_ms
            ) VALUES (
              %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 1, %s, %s, %s, %s, %s,
              %s::jsonb, %s::jsonb, %s, %s, %s, %s, %s, %s, %s
            )
            """,
            (
                event_id,
                leader_item_id,
                dedupe_family,
                event_kind,
                source_contract_reason,
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
                queue_priority,
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
                INSERT INTO news_event_bands (band_index, band_key, event_id, dedupe_family, expires_at_ms)
                SELECT q.band_index, q.band_key, %s, %s, %s
                  FROM unnest(%s::smallint[], %s::text[]) AS q(band_index, band_key)
                ON CONFLICT DO NOTHING
                """,
                (event_id, dedupe_family, int(expires_at_ms), list(range(len(band_keys))), list(band_keys)),
            )
        for symbol in grounded_assets:
            self.conn.execute(
                """
                INSERT INTO news_event_assets (symbol, event_id, market_type, opened_at_ms)
                VALUES (%s, %s, NULL, %s) ON CONFLICT DO NOTHING
                """,
                (symbol.upper().replace("XYZ-", ""), event_id, int(opened_at_ms)),
            )

    def record_event_assets(self, *, event_id: str, assets: Sequence[tuple[str, str | None]]) -> None:
        """Attach symbols a *deterministic* judge resolved to an Event the Gate could not ground (#267).

        `news_event_assets` answers "which assets does this Event concern", and four planes read it:
        the Reaction planner's due scan, the feed's `?symbol=` filter behind the token page, the
        instrument-grounding funnel, and reader history's canonical-asset overlap. The telemetry lanes
        were absent from all four for the same reason — an OI frame's wire text is
        `NVDA OI Rise 4.55%, OI Value 32.17M, …`, which the admission Gate cannot ground, so
        `grounded_assets` is `[]` and no row was ever written. The canonical symbol exists a moment
        later, when the deterministic parser resolves it, and until now nothing carried it back here.

        The anchor is read from the Event row rather than passed in, so the column cannot disagree
        with the Event whose reaction horizons are measured from it.

        `ON CONFLICT DO NOTHING` keeps this idempotent under redelivery, which matters because Triage
        can settle the same Event twice; the row is identical either way.
        """

        rows = [(str(symbol or "").strip().upper().removeprefix("XYZ-"), market_type) for symbol, market_type in assets]
        rows = [row for row in rows if row[0]]
        if not rows:
            return
        self.conn.execute(
            """
            INSERT INTO news_event_assets (symbol, event_id, market_type, opened_at_ms)
            SELECT q.symbol, e.event_id, q.market_type, e.opened_at_ms
              FROM news_events e, unnest(%s::text[], %s::text[]) AS q(symbol, market_type)
             WHERE e.event_id = %s
            ON CONFLICT DO NOTHING
            """,
            ([row[0] for row in rows], [row[1] for row in rows], event_id),
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
               AND (
                 SELECT s.provenance = 'observed'
                    AND s.snapshot ->> 'schema_version' = 'news_event_evidence_v3'
                   FROM news_event_evidence_snapshots s
                  WHERE s.event_id = news_events.event_id
                  ORDER BY s.evidence_version DESC LIMIT 1
               )
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
               AND (
                 SELECT s.provenance = 'observed'
                    AND s.snapshot ->> 'schema_version' = 'news_event_evidence_v3'
                   FROM news_event_evidence_snapshots s
                  WHERE s.event_id = news_events.event_id
                  ORDER BY s.evidence_version DESC LIMIT 1
               )
            """,
            (int(older_than_ms),),
        ).fetchone()
        return int(row["n"]) if row else 0

    def upgrade_event_admission(
        self,
        *,
        event_id: str,
        admission: str,
        queue_priority: str,
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
               SET admission = %s, queue_priority = %s, asset_class = %s, grounded_assets = %s::jsonb,
                   watchlist_hits = %s::jsonb, macro_lexicon = %s, updated_at_ms = %s
             WHERE event_id = %s
             RETURNING opened_at_ms
            """,
            (
                admission,
                queue_priority,
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
        if latest is not None and (
            str(latest["provenance"]) != "observed"
            or str(dict(latest["snapshot"] or {}).get("schema_version") or "") != "news_event_evidence_v3"
        ):
            raise ValueError("news_event_archive_only")
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
                "dedupe_family",
                "event_kind",
                "source_contract_reason",
                "comparison_fingerprint",
                "comparison_title",
                "opened_at_ms",
                "last_member_at_ms",
                "expires_at_ms",
                "member_count",
                "admission",
                "queue_priority",
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
            "schema_version": "news_event_evidence_v3",
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
             WHERE event_id = %s
               AND provenance = 'observed'
               AND snapshot ->> 'schema_version' = 'news_event_evidence_v3'
             ORDER BY evidence_version DESC LIMIT 1
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
        if snapshot.get("schema_version") != "news_event_evidence_v3":
            raise ValueError("news_event_evidence_contract_invalid")
        card = dict(snapshot.get("card") or {})
        card.update(
            {
                "evidence_schema_version": str(snapshot.get("schema_version") or ""),
                "evidence_version": int(evidence["evidence_version"]),
                "evidence_sha256": str(evidence["evidence_sha256"]),
                "focus_fact_id": str(evidence["focus_fact_id"]),
                "evidence_provenance": str(evidence["provenance"]),
                "evidence_release_eligible": bool(evidence["release_eligible"]),
            }
        )
        return card

    def fact_membership(
        self,
        *,
        item_id: str,
        fact_id: str,
        event_kind: EventKind,
    ) -> Mapping[str, Any] | None:
        """Return the stable same-kind Event assignment for one admitted FactUnit."""

        return cast(
            Mapping[str, Any] | None,
            self.conn.execute(
                """
                SELECT m.event_id, m.match_kind
                 FROM news_event_members m
                  JOIN news_events e ON e.event_id = m.event_id
                 WHERE m.item_id = %s AND m.fact_id = %s AND e.event_kind = %s
                 ORDER BY e.opened_at_ms LIMIT 1
                """,
                (item_id, fact_id, event_kind),
            ).fetchone(),
        )

    def event_admission(self, event_id: str) -> Mapping[str, Any] | None:
        """Material routing identity needed for idempotent FactUnit redelivery."""

        return cast(
            Mapping[str, Any] | None,
            self.conn.execute(
                "SELECT admission, event_kind, storyline_key FROM news_events WHERE event_id = %s",
                (event_id,),
            ).fetchone(),
        )

    def event_delivery_timing(self, event_id: str) -> Mapping[str, Any] | None:
        """Source time, Reaction anchor, and local observation for reader-facing delivery."""

        row = self.conn.execute(
            """
            SELECT i.published_at_ms AS news_at_ms, e.opened_at_ms AS reaction_anchor_at_ms,
                   i.observed_at_ms, i.canonical_url
              FROM news_events e JOIN news_items i ON i.item_id = e.leader_item_id
             WHERE e.event_id = %s
            """,
            (event_id,),
        ).fetchone()
        if row is None:
            return None
        data = dict(row)
        _, artifact_at_ms = source_artifact_identity(str(data.get("canonical_url") or ""))
        return {
            "news_at_ms": int(artifact_at_ms or data["news_at_ms"]),
            # Every news_event_assets row is anchored to the Event's opened_at_ms. Reaction rows are only
            # materialized when a horizon is due, so delivery needs this durable anchor to distinguish
            # "not due" from "due but still pending" before the first Reaction row exists.
            "reaction_anchor_at_ms": int(data["reaction_anchor_at_ms"]),
            "observed_at_ms": int(data["observed_at_ms"]),
        }

    def resolve_unverified_source_contract(
        self,
        *,
        event_id: str,
        reason: SourceContractReason | None,
        now_ms: int,
    ) -> None:
        """Settle a pre-cut deterministic Event after the current strict parser runs."""

        if reason not in {None, "source_contract_drift"}:
            raise ValueError("news_source_contract_resolution_invalid")
        self.conn.execute(
            """
            UPDATE news_events
               SET source_contract_reason = %s, updated_at_ms = %s
             WHERE event_id = %s AND source_contract_reason = 'source_contract_unverified'
            """,
            (reason, int(now_ms), event_id),
        )

    def event_regate_context(self, event_id: str) -> Mapping[str, Any] | None:
        """Leader evidence needed to decide whether a later Event member is stronger."""

        return cast(
            Mapping[str, Any] | None,
            self.conn.execute(
                """
                SELECT e.admission, e.storyline_key, e.published_at_ms,
                       i.reporting_origin AS leader_origin, i.provider_metadata AS leader_provider_metadata
                  FROM news_events e JOIN news_items i ON i.item_id = e.leader_item_id
                 WHERE e.event_id = %s
                """,
                (event_id,),
            ).fetchone(),
        )

    def item_provider_score(self, item_id: str) -> Any:
        """Raw provider score used only for deterministic stronger-member comparison."""

        return self.conn.execute(
            "SELECT provider_metadata ->> 'score' AS score FROM news_items WHERE item_id = %s",
            (item_id,),
        ).fetchone()
