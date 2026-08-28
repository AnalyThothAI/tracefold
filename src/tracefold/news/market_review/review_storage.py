"""Bounded release-cohort Market Review aggregates and miss discovery."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from ..outcome import decision_zh, direction_zh, event_type_zh, magnitude_zh, override_rule_zh, throttled_by_zh
from ..similarity import similarity
from .pricing import (
    REACTION_HISTORY_MAX_AGE_MS,
    REACTION_METRIC_VERSION,
    REVIEW_POTENTIAL_MISS_LIMIT,
    median_bps,
    quote_freshness,
)
from .projections import _coverage_rows, _optional_int, _reaction_public

_REVIEW_DISCOVERY_MAX_HOURS: Final = 168


@dataclass(frozen=True, slots=True)
class MarketReviewCohort:
    """Exact release identity plus the human-readable verdict labels it selected."""

    bundle_sha256: str
    program_version: str
    program_sha256: str
    policy_version: str
    model: str

    @property
    def label(self) -> str:
        return "/".join((self.program_version, self.policy_version, self.model))


_REVIEW_FACTS_CTE: Final = """
    -- One flat pass, deliberately. Two things make this query fast enough to live under Serve's one-second
    -- statement timeout at the 720 h bound (#88 §14): the latest verdict per Event is picked in the same scan
    -- that filters the window, so the planner estimates from table statistics instead of guessing 250 rows
    -- for a CTE chain and choosing nested loops; and the sort carries only what the aggregates read — the
    -- long text an Event needs on screen is fetched afterwards for the fifty rows that reach the page.
    ev AS (
      SELECT DISTINCT ON (v.event_id)
             v.event_id, e.opened_at_ms,
             e.opened_at_ms <= %s - 3600000 AS mature_1h,
             e.opened_at_ms <= %s - 14400000 AS mature_4h,
             v.final_decision, v.degraded, v.override_rule, v.throttled_by,
             v.verdict ->> 'direction' AS direction,
             COALESCE((v.verdict ->> 'magnitude')::int, 0) AS magnitude,
             COALESCE(v.verdict ->> 'event_type', 'other') AS event_type,
             (d.state = 'sent') AS delivered
        FROM news_verdicts v
        JOIN news_events e ON e.event_id = v.event_id
        LEFT JOIN news_deliveries d ON d.event_id = v.event_id AND d.kind = 'first'
       WHERE v.stage = 'triage' AND e.ingest_mode = 'live'
         AND e.opened_at_ms >= %s AND e.opened_at_ms < %s
         {cohort_filter}
       ORDER BY v.event_id, v.created_at_ms DESC
    ),
    agg AS (
      SELECT r.event_id,
             count(*) AS asset_n,
             count(r.return_1h_bps) AS priced_1h,
             count(r.return_4h_bps) AS priced_4h,
             count(*) FILTER (WHERE r.state = 'unavailable') AS unavailable_n,
             min(r.unavailable_reason) AS unavailable_reason,
             (array_agg(r.return_1h_bps ORDER BY r.return_1h_bps)
                FILTER (WHERE r.return_1h_bps IS NOT NULL AND r.anchor_at_ms >= %s))[
                  (count(r.return_1h_bps) FILTER (WHERE r.anchor_at_ms >= %s) + 1) / 2
                ] AS bps_1h,
             NULL::integer AS bps_4h
        FROM news_event_reactions r
       WHERE r.metric_version = %s AND r.is_primary
         AND r.anchor_at_ms >= %s AND r.anchor_at_ms < %s
       GROUP BY r.event_id
    ),
    fact AS (
      SELECT ev.event_id, ev.opened_at_ms, ev.mature_1h, ev.mature_4h,
             ev.final_decision, ev.degraded, ev.override_rule, ev.throttled_by,
             ev.direction, ev.magnitude, ev.event_type, ev.delivered,
             a.event_id IS NOT NULL AS has_primary,
             COALESCE(a.asset_n, 0) AS asset_n,
             COALESCE(a.priced_1h, 0) AS priced_1h,
             COALESCE(a.priced_4h, 0) AS priced_4h,
             a.unavailable_reason, a.bps_1h, a.bps_4h
        FROM ev LEFT JOIN agg a ON a.event_id = ev.event_id
    )
"""


class ReviewStorage:
    conn: Any

    def review(
        self,
        *,
        hours: int,
        now_ms: int,
        cohort: MarketReviewCohort | None = None,
    ) -> dict[str, Any]:
        """The whole 命中复盘 payload for one bounded window. Coverage first, then accuracy.

        One pass, not five. The shared fact set — every live Event in the window with its latest Triage
        verdict and its event-level aggregate — is expensive enough at the 720 h bound that re-deriving it per
        section cost 3.7 s against a 250 ms budget (#88 §14). PostgreSQL materializes a CTE referenced more
        than once, so the sections below read it instead of rebuilding it, and the whole page is one round
        trip that returns tens of rows rather than the window.
        """

        sql, params, start_ms, end_ms, discovery_start_ms = self.review_statement(
            hours=hours,
            now_ms=now_ms,
            cohort=cohort,
        )
        sections = self._review_sections(sql, params)
        coverage = _coverage_rows(sections["coverage"][0] if sections["coverage"] else {})
        return {
            "meta": {
                "hours": int(hours),
                "window_start_ms": start_ms,
                "window_end_ms": end_ms,
                "discovery_window_start_ms": discovery_start_ms,
                "metric_version": REACTION_METRIC_VERSION,
                "measured_at_ms": int(now_ms),
                "cohort": cohort.label if cohort else None,
                "cohort_sha256": cohort.bundle_sha256 if cohort else None,
                "program_sha256": cohort.program_sha256 if cohort else None,
            },
            "coverage": coverage,
            "directions": [],
            # Retired from the product surface in #112: these post-event price rankings were not causal
            # quality evidence, and their ordered aggregates dominated the 30-day query budget.
            "magnitudes": [],
            "event_types": [],
            "potential_misses": self._miss_rows(sections["miss"]),
            "summary": {
                "hit_1h_pct": None,
                "hit_1h_n": 0,
                "coverage_1h_pct": (coverage[0] if coverage else {}).get("coverage_pct"),
            },
        }

    def _review_sections(self, sql: str, params: tuple[Any, ...]) -> dict[str, list[dict[str, Any]]]:
        """Every section of the page in one statement, tagged by section and carried as JSON rows."""

        rows = self.conn.execute(sql, params).fetchall()
        out: dict[str, list[dict[str, Any]]] = {
            "coverage": [],
            "miss": [],
        }
        for row in rows:
            payload = row["payload"]
            # psycopg hands jsonb back as a mapping when a loader is registered and as text otherwise; the
            # section rows are small, so accept either rather than depending on connection setup.
            out[str(row["section"])].append(dict(json.loads(payload) if isinstance(payload, str) else payload))
        return out

    @staticmethod
    def review_statement(
        *,
        hours: int,
        now_ms: int,
        cohort: MarketReviewCohort | None,
    ) -> tuple[str, tuple[Any, ...], int, int, int]:
        """Build the exact bounded market-review read used by serving and query audit."""

        window_ms = int(hours) * 3_600_000
        start_ms, end_ms = int(now_ms) - window_ms, int(now_ms)
        discovery_start_ms = max(start_ms, end_ms - _REVIEW_DISCOVERY_MAX_HOURS * 3_600_000)
        params: list[Any] = [end_ms, end_ms, start_ms, end_ms]
        cohort_filter = ""
        if cohort is not None:
            cohort_filter = (
                "AND COALESCE(v.trace #>> '{agent_assignment,bundle_sha}', '') = %s "
                "AND COALESCE(v.program_version, '') = %s "
                "AND COALESCE(v.program_sha256, '') = %s "
                "AND COALESCE(v.policy_version, '') = %s "
                "AND COALESCE(v.model, '') = %s"
            )
            params.extend(
                (
                    cohort.bundle_sha256,
                    cohort.program_version,
                    cohort.program_sha256,
                    cohort.policy_version,
                    cohort.model,
                )
            )
        params.extend(
            (
                discovery_start_ms,
                discovery_start_ms,
                REACTION_METRIC_VERSION,
                start_ms,
                end_ms,
            )
        )
        facts_cte = _REVIEW_FACTS_CTE.format(cohort_filter=cohort_filter)
        sql = f"""
            WITH {facts_cte},
            -- One coverage aggregate and one bounded discovery queue over one materialized fact set.  #112
            -- retired the direction/magnitude/event-type price rankings: they were neither causal quality
            -- evidence nor rendered by ReviewDesk, and they consumed the 30-day query budget.
            coverage AS (
              SELECT count(*) FILTER (WHERE mature_1h) AS eligible_1h,
                     count(*) FILTER (WHERE mature_4h) AS eligible_4h,
                     count(*) FILTER (WHERE mature_1h AND priced_1h > 0) AS priced_1h,
                     count(*) FILTER (WHERE mature_4h AND priced_4h > 0) AS priced_4h,
                     count(*) FILTER (WHERE mature_1h AND NOT has_primary) AS no_primary_1h,
                     count(*) FILTER (WHERE mature_4h AND NOT has_primary) AS no_primary_4h,
                     count(*) FILTER (WHERE mature_1h AND degraded) AS degraded_1h,
                     count(*) FILTER (WHERE mature_4h AND degraded) AS degraded_4h,
                     count(*) FILTER (
                       WHERE mature_1h AND unavailable_reason = 'instrument_unresolved'
                     ) AS unresolved_1h,
                     count(*) FILTER (
                       WHERE mature_4h AND unavailable_reason = 'instrument_unresolved'
                     ) AS unresolved_4h,
                     count(*) FILTER (WHERE mature_1h AND unavailable_reason = 'no_candle_within_gap') AS gap_1h,
                     count(*) FILTER (WHERE mature_4h AND unavailable_reason = 'no_candle_within_gap') AS gap_4h,
                     count(*) FILTER (WHERE mature_1h AND unavailable_reason = 'history_expired') AS expired_1h,
                     count(*) FILTER (WHERE mature_4h AND unavailable_reason = 'history_expired') AS expired_4h,
                     count(*) FILTER (WHERE mature_1h AND unavailable_reason = 'reference_only') AS reference_1h,
                     count(*) FILTER (WHERE mature_4h AND unavailable_reason = 'reference_only') AS reference_4h
                FROM fact
            ),
            miss AS (
              SELECT event_id, opened_at_ms, final_decision, override_rule, throttled_by, direction,
                     magnitude, event_type, bps_1h, bps_4h, asset_n
                FROM fact
               WHERE COALESCE(delivered, false) IS NOT TRUE AND NOT degraded AND bps_1h IS NOT NULL
               ORDER BY abs(bps_1h) DESC, opened_at_ms DESC
               LIMIT {int(REVIEW_POTENTIAL_MISS_LIMIT)}
            )
            -- Each CTE is aliased before `to_jsonb`: a bare CTE name that also names one of its columns
            -- resolves to the column, and `to_jsonb(direction)` silently returned the string 'bullish'.
            SELECT 'coverage' AS section, to_jsonb(c) AS payload FROM coverage c
            UNION ALL SELECT 'miss', to_jsonb(x) FROM miss x
            """
        return sql, tuple(params), start_ms, end_ms, discovery_start_ms

    def _miss_rows(self, misses: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Withheld Events ranked by how much the market moved afterwards — a queue, never a verdict.

        Price movement does not prove the Event caused the move or that it should have been pushed, so every
        row carries the decision and the named rule that produced it and nothing here writes a label.
        """

        event_ids = [str(row["event_id"]) for row in misses]
        assets = self._reaction_assets_for(event_ids)
        text = self._miss_text(event_ids)
        rows: list[dict[str, Any]] = [
            {
                "event_id": str(data["event_id"]),
                "opened_at_ms": int(data["opened_at_ms"]),
                "headline_zh": (
                    text.get(str(data["event_id"]), {}).get("headline_zh")
                    or text.get(str(data["event_id"]), {}).get("leader_title")
                ),
                "leader_title": str(text.get(str(data["event_id"]), {}).get("leader_title") or ""),
                "storyline_key": str(text.get(str(data["event_id"]), {}).get("storyline_key") or ""),
                "final_decision": str(data.get("final_decision") or ""),
                "decision_zh": decision_zh(data.get("final_decision")),
                "override_rule": data.get("override_rule"),
                "override_rule_zh": override_rule_zh(data.get("override_rule")),
                "throttled_by": data.get("throttled_by"),
                "throttled_by_zh": throttled_by_zh(data.get("throttled_by")),
                "direction": data.get("direction"),
                "direction_zh": direction_zh(data.get("direction")),
                "magnitude": _optional_int(data.get("magnitude")),
                "magnitude_zh": magnitude_zh(_optional_int(data.get("magnitude"))),
                "event_type": data.get("event_type"),
                "event_type_zh": event_type_zh(data.get("event_type")),
                "return_1h_bps": _optional_int(data.get("bps_1h")),
                "return_4h_bps": median_bps(
                    [
                        int(asset["return_4h_bps"])
                        for asset in assets.get(str(data["event_id"]), [])
                        if asset.get("is_primary") and asset.get("return_4h_bps") is not None
                    ]
                ),
                "asset_n": int(data.get("asset_n") or 0),
                "assets": assets.get(str(data["event_id"]), []),
            }
            for data in misses
        ]
        # The market queue is about distinct facts, not how many provider Items happened to become Events.
        # N is capped at 50, so an explicit pairwise connected-component fold is simpler and more auditable
        # than adding another persistence model.  Similar wording within four hours is one discovery case;
        # the highest-move row remains the representative because `miss` already arrives in that order.
        parents = list(range(len(rows)))

        def root(index: int) -> int:
            while parents[index] != index:
                parents[index] = parents[parents[index]]
                index = parents[index]
            return index

        def union(left: int, right: int) -> None:
            a, b = root(left), root(right)
            if a != b:
                parents[b] = a

        for left, first in enumerate(rows):
            first_text = str(first.get("headline_zh") or first.get("leader_title") or "")
            for right in range(left + 1, len(rows)):
                second = rows[right]
                if abs(int(first["opened_at_ms"]) - int(second["opened_at_ms"])) > 4 * 3_600_000:
                    continue
                second_text = str(second.get("headline_zh") or second.get("leader_title") or "")
                if similarity(first_text, second_text) >= 0.55:
                    union(left, right)

        groups: dict[int, list[int]] = {}
        for index in range(len(rows)):
            groups.setdefault(root(index), []).append(index)
        clustered: list[dict[str, Any]] = []
        for members in sorted(groups.values(), key=min):
            representative = dict(rows[min(members)])
            event_ids = sorted(str(rows[index]["event_id"]) for index in members)
            representative["fact_cluster_key"] = hashlib.sha256("|".join(event_ids).encode()).hexdigest()
            representative["fact_cluster_n"] = len(event_ids)
            representative["related_event_ids"] = event_ids
            clustered.append(representative)
        return clustered

    def _miss_text(self, event_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
        """Reader text for the rows that actually reach the page — never carried through the window's sort."""

        if not event_ids:
            return {}
        rows = self.conn.execute(
            """
            SELECT e.event_id, e.leader_title, e.storyline_key,
                   (SELECT v.verdict ->> 'headline_zh' FROM news_verdicts v
                     WHERE v.event_id = e.event_id AND v.stage = 'triage'
                     ORDER BY v.created_at_ms DESC LIMIT 1) AS headline_zh
              FROM news_events e
             WHERE e.event_id = ANY(%s)
            """,
            (list(event_ids),),
        ).fetchall()
        return {str(row["event_id"]): dict(row) for row in rows}

    def _reaction_assets_for(self, event_ids: Sequence[str]) -> dict[str, list[dict[str, Any]]]:
        if not event_ids:
            return {}
        rows = self.conn.execute(
            """
            SELECT event_id, symbol, metric_version, venue, venue_symbol, instrument_class, anchor_at_ms,
                   p0, p0_at_ms, p1, p1_at_ms, p4, p4_at_ms, return_1h_bps, return_4h_bps,
                   is_primary, state, unavailable_reason, updated_at_ms
              FROM news_event_reactions
             WHERE event_id = ANY(%s) AND metric_version = %s
             ORDER BY event_id, symbol
            """,
            (list(event_ids), REACTION_METRIC_VERSION),
        ).fetchall()
        out: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            data = dict(row)
            out.setdefault(str(data["event_id"]), []).append(_reaction_public(data))
        return out

    def price_status(self, *, now_ms: int) -> dict[str, Any]:
        """What an operator needs before the UI shows it: source freshness and Reaction backlog."""

        snapshots = self.quote_snapshots()  # type: ignore[attr-defined]
        sources: list[dict[str, Any]] = []
        for key, row in sorted(snapshots.items()):
            received_at_ms = int(row["received_at_ms"])
            entries = [entry for entry in (row.get("quotes") or {}).values() if isinstance(entry, Mapping)]
            freshness = [
                quote_freshness(
                    measured_at_ms=now_ms,
                    received_at_ms=received_at_ms,
                    source_at_ms=_optional_int(entry.get("source_at_ms")),
                )
                for entry in entries
            ]
            source_ages = [item.source_age_ms for item in freshness if item.source_age_ms is not None]
            source_times = [
                value for entry in entries if (value := _optional_int(entry.get("source_at_ms"))) is not None
            ]
            sources.append(
                {
                    "source_key": key,
                    "target_count": int(row.get("target_count") or 0),
                    "quote_count": len(entries),
                    "received_age_ms": max((item.received_age_ms for item in freshness), default=None),
                    "source_age_ms": max(source_ages, default=None),
                    "effective_age_ms": max((item.effective_age_ms for item in freshness), default=None),
                    "freshness_basis": (
                        "source_and_received" if source_ages else "received_only" if freshness else None
                    ),
                    "state": (
                        "stale"
                        if any(item.state == "stale" for item in freshness)
                        else "fresh"
                        if freshness
                        else "unavailable"
                    ),
                    "source_at_ms": min(source_times, default=None),
                    "received_at_ms": received_at_ms,
                }
            )
        row = self.conn.execute(
            """
            SELECT count(*) FILTER (WHERE state = 'partial') AS partial_n,
                   count(*) FILTER (WHERE state = 'complete') AS complete_n,
                   count(*) FILTER (WHERE state = 'unavailable') AS unavailable_n
              FROM news_event_reactions
             WHERE metric_version = %s AND anchor_at_ms >= %s
            """,
            (REACTION_METRIC_VERSION, int(now_ms) - 7 * 24 * 3_600_000),
        ).fetchone()
        counts = dict(row or {})
        return {
            "metric_version": REACTION_METRIC_VERSION,
            # The backlog SLO (#88 §14) is oldest-due age, not loop frequency: a turn can run on time and
            # still fall behind. Reporting it is what makes "healthy under 5 minutes" observable at all.
            "oldest_due_age_ms": self.oldest_due_age_ms(  # type: ignore[attr-defined]
                now_ms=now_ms, history_max_age_ms=REACTION_HISTORY_MAX_AGE_MS
            ),
            "sources": sources,
            "fresh_sources": sum(1 for source in sources if source["state"] == "fresh"),
            "quotes": sum(source["quote_count"] for source in sources),
            "reaction_partial_7d": int(counts.get("partial_n") or 0),
            "reaction_complete_7d": int(counts.get("complete_n") or 0),
            "reaction_unavailable_7d": int(counts.get("unavailable_n") or 0),
        }
