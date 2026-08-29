"""Bounded feed, detail, status, and public projection reads."""

from __future__ import annotations

import base64
import time
from collections.abc import Mapping, Sequence
from typing import Any, Final

from ..models import ReaderReceipt, display_title
from ..oi_signals import METRIC_VERSION as OI_METRIC_VERSION
from ..oi_signals import PROGRAM_VERSION as OI_PROGRAM_VERSION
from ..outcome import (
    audience_zh,
    decision_zh,
    direction_zh,
    event_outcome,
    event_type_zh,
    magnitude_zh,
    novelty_zh,
    scope_zh,
)
from ..source_contracts import (
    EVENT_KINDS,
    OI_SOURCE_IDENTITY,
    SOURCE_CONTRACT_CLASSIFIER_VERSION,
    SOURCE_CONTRACT_FAMILIES,
    EventKind,
)
from ..timeline import event_timeline
from .sql_values import _ADMITTED_SQL

# Feed task tabs mirror OUTCOME_GROUP in outcome.py over the feed's joined rows.
_PENDING_CORE_SQL: Final = (
    "COALESCE(d.state = 'sending', false) OR ("
    "d.state IS NULL"
    f" AND e.admission IN ({_ADMITTED_SQL})"
    " AND (t.final_decision IS NULL OR t.final_decision IN ('push', 'escalate')))"
)
_OUTCOME_GROUP_SQL: Final = {
    "pushed": "d.state = 'sent'",
    "pending": f"COALESCE(d.state, '') <> 'sent' AND ({_PENDING_CORE_SQL})",
    "held": f"COALESCE(d.state, '') <> 'sent' AND NOT ({_PENDING_CORE_SQL})",
}

# The deterministic OI lane's own rule names, grouped the three ways the monitor asks about them. These are
# `oi_signals.evaluate_oi` / `oi_parse_failure` keys verbatim: the reader-facing tab has no vocabulary of its
# own, and a rule the judge adds shows up as an unfiltered row rather than silently joining `withheld`.
_OI_PUSH_RULE: Final = "opening_move_with_whale_concentration"
_OI_WITHHELD_RULES: Final = ("whale_ratio_below_threshold", "oi_change_below_threshold", "beyond_window_rank")
_OI_PARSE_FAILED_RULE: Final = "oi_parse_failed"
OI_FILTERS: Final[dict[str, tuple[str, ...]]] = {
    "pushed": (_OI_PUSH_RULE,),
    "withheld": _OI_WITHHELD_RULES,
    "parse_failed": (_OI_PARSE_FAILED_RULE,),
}
# `all` is the monitor's fourth tab and narrows nothing, so it is not in `OI_FILTERS`. It still has to be a
# value the caller can send: it is how a request says "I am the 持仓异动 monitor", which is what lets the
# outcome-group count be skipped for the tab that is displayed most.
OI_OUTCOMES: Final[frozenset[str]] = frozenset({"all", *OI_FILTERS})
# Both feed laterals expose the judged rule under this one alias, so the page query and the tab-count query
# share the predicate verbatim. A count that filtered differently from the rows it counts is the whole bug
# this alias exists to prevent.
_OI_RULE_SQL: Final = "v.trace -> 'oi_signal' ->> 'rule' AS oi_rule"
# The lane the key can only come from. `triage.py` enters its OI branch under
# `admission == 'telemetry_deterministic'` and nothing else writes `trace['oi_signal']`, so pairing the rule
# with the admission narrows nothing semantically — measured live on 2026-08-25, all 298 verdicts carrying
# the key across the whole retention belonged to that admission.
#
# It is not decoration. Without it the planner has to read `trace` — a TOASTed JSONB column, 26 MB over 24 h
# of verdicts — for every candidate row before it can tell whether the rule matches. A rare rule finds no
# rows to stop early on, so `?oi=parse_failed` with no window walked the entire retention and hit the serve
# role's 1 s `statement_timeout`: measured 500 in 1.24 s unbounded, against 0.44 s with the admission and
# 9 ms on the 24 h window the console actually sends. Pairing them puts the admission index in front of the
# detoast.
_OI_RULE_PREDICATE: Final = "e.admission = 'telemetry_deterministic' AND t.oi_rule = ANY(%s)"


class FeedStorage:
    conn: Any

    def list_feed(
        self,
        *,
        family: str | None,
        admission: str | None,
        decision: str | None,
        symbol: str | None,
        q: str | None,
        limit: int,
        cursor: str | None,
        outcome: str | None = None,
        hours: int | None = None,
        oi: str | None = None,
        directions: tuple[str, ...] | None = None,
        channels: tuple[EventKind, ...] | None = None,
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
        if symbol:
            # The filter names an *identity*, not one spelling. `news_event_assets` stores the normalized
            # provider tag, so an Event the Gate grounded on `SKHX` carries `SKHX` — but #87 collapsed SKHX,
            # SKHY and SKHYNIX into one base, the asset chip renders that base, and the token page is keyed
            # on it. Matching the tag exactly would leave the Event that supplied the link missing from its
            # own symbol page.
            #
            # The alias lookup is a primary-key probe on `news_symbol_aliases` per candidate row, which is
            # why it sits inside the same EXISTS rather than expanding to a list the caller has to build.
            where.append(
                "EXISTS (SELECT 1 FROM news_event_assets a"
                " WHERE a.event_id = e.event_id"
                "   AND (a.symbol = %s"
                "        OR COALESCE((SELECT n.base_symbol FROM news_symbol_aliases n WHERE n.alias = a.symbol), '')"
                "            = %s))"
            )
            params.extend([symbol.upper(), symbol.upper()])
        if q:
            pattern = f"%{q}%"
            where.append(
                "(e.search_doc @@ plainto_tsquery('simple', %s)"
                " OR e.leader_title ILIKE %s"
                " OR i.reporting_origin ILIKE %s"
                " OR EXISTS ("
                "   SELECT 1 FROM news_event_assets a"
                "   LEFT JOIN news_symbol_aliases n ON n.alias = a.symbol"
                "   LEFT JOIN news_market_instruments m"
                "     ON m.base_symbol = COALESCE(n.base_symbol, a.symbol)"
                "  WHERE a.event_id = e.event_id"
                "    AND (a.symbol ILIKE %s OR COALESCE(n.base_symbol, '') ILIKE %s"
                "         OR COALESCE(m.venue, '') ILIKE %s"
                "         OR COALESCE(m.venue_symbol, '') ILIKE %s)"
                "))"
            )
            params.extend([q, pattern, pattern, pattern, pattern, pattern, pattern])
        if decision:
            where.append("t.final_decision = %s")
            params.append(decision)
        if directions:
            where.append("t.direction = ANY(%s)")
            params.append(list(directions))
        if channels and len(channels) < len(EVENT_KINDS):
            where.append("e.event_kind = ANY(%s)")
            params.append(list(channels))
        if oi in OI_FILTERS:
            # The judge's own rule, from the trace it wrote. `final_decision` cannot answer this: a frame held
            # by a threshold and one whose provider template stopped parsing are both `drop`, and both carry
            # `override_rule = 'telemetry_deterministic'` because `decide()` names the admission, not the gate.
            # Measured live on 2026-08-25: all 99 OI verdicts in 24 h carried that one `override_rule`, while
            # the trace split them 78 / 19 / 2 across the three gates. The predicate carries the lane's
            # admission with the rule, which is what keeps it off the whole retention's TOAST — see
            # `_OI_RULE_PREDICATE`.
            where.append(_OI_RULE_PREDICATE)
            params.append(list(OI_FILTERS[oi]))
        # Counting is worth one extra aggregate only on the first page; later pages reuse what it returned.
        # Snapshot the clauses so the outcome group and cursor appended below cannot reach the count query.
        #
        # Any `oi` request skips it too — including `all`, which narrows nothing but still identifies the
        # caller as the OI monitor. `counts` describes the three *outcome* groups, which are the feed's task
        # tabs; the monitor's tabs are gates and it takes their counts from `status.oi`, so on a 5 s poll
        # this would be the file's own "19 ms over the entire table" aggregate run for a field nobody reads.
        wants_counts = cursor_opened is None and oi not in OI_OUTCOMES
        counts = self._feed_counts(where=list(where), params=list(params)) if wants_counts else None
        if outcome in _OUTCOME_GROUP_SQL:
            where.append(_OUTCOME_GROUP_SQL[outcome])
        if cursor_opened is not None:
            where.append("(e.opened_at_ms, e.event_id) < (%s, %s)")
            params.extend([cursor_opened, cursor_id])
        rows = self.conn.execute(
            f"""
            SELECT e.event_id, e.family, e.event_kind, e.source_contract_reason, e.leader_title,
                   e.opened_at_ms, e.last_member_at_ms, e.member_count,
                   e.admission, e.provider_score_max, e.engine_type, e.asset_class, e.grounded_assets,
                   e.watchlist_hits, e.storyline_key, e.context_line, e.published_at_ms, e.ingest_mode,
                   i.canonical_url AS leader_url, i.reporting_origin, i.provenance,
                   t.final_decision, t.override_rule, t.throttled_by, t.degraded AS triage_degraded,
                   t.error_code AS triage_error_code,
                   t.verdict ->> 'direction' AS direction, (t.verdict ->> 'magnitude')::int AS magnitude,
                   t.verdict ->> 'event_type' AS event_type, t.verdict ->> 'headline_zh' AS headline_zh,
                   t.verdict ->> 'scope' AS scope, t.verdict ->> 'title_zh' AS title_zh,
                   t.model_decision, t.verdict AS triage_verdict, t.trace -> 'oi_signal' AS oi_signal,
                   d.state AS delivery_state, d.settled_at_ms AS delivered_at_ms, d.error_code AS delivery_error_code
              FROM news_events e
              JOIN news_items i ON i.item_id = e.leader_item_id
              LEFT JOIN LATERAL (
                SELECT v.*, v.verdict ->> 'direction' AS direction, {_OI_RULE_SQL}
                  FROM news_verdicts v
                 WHERE v.event_id = e.event_id AND v.stage = 'triage'
                 ORDER BY v.created_at_ms DESC LIMIT 1
              ) t ON true
              LEFT JOIN news_deliveries d ON d.event_id = e.event_id AND d.kind = 'first'
             WHERE {" AND ".join(where)}
             ORDER BY e.opened_at_ms DESC, e.event_id DESC
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
                "decision": decision,
                "symbol": symbol,
                "q": q,
                "limit": int(limit),
                "outcome": outcome if outcome in _OUTCOME_GROUP_SQL else None,
                "hours": window_hours,
                "oi": oi if oi in OI_OUTCOMES else None,
                "direction": ",".join(directions) if directions else None,
                "channel": ",".join(channels) if channels else None,
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
                SELECT v.final_decision, v.verdict ->> 'direction' AS direction, {_OI_RULE_SQL}
                  FROM news_verdicts v
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
        card = self._current_event_card(event_id)  # type: ignore[attr-defined]
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
                "pending_card": dict(r["pending_card"]) if r["pending_card"] is not None else None,
                "receipt": r["receipt"],
                "edit_state": r["edit_state"],
                "edit_error_code": r["edit_error_code"],
                "edit_attempted_at_ms": r["edit_attempted_at_ms"],
                "edit_settled_at_ms": r["edit_settled_at_ms"],
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

    def _oi_telemetry_24h(self, *, day_ago: int) -> dict[str, int]:
        row = self.conn.execute(
            """
            SELECT
              (SELECT count(*) FROM news_items
                WHERE observed_at_ms >= %s
                  AND EXISTS (
                    SELECT 1
                      FROM jsonb_array_elements(COALESCE(provider_metadata -> 'strategies', '[]'::jsonb)) s
                     WHERE s ->> 'id' = %s
                       AND s ->> 'name' = %s
                       AND s ->> 'source_type' = %s
                       AND s ->> 'engine_type' = %s
                  )
              ) AS telemetry_received_24h,
              (SELECT count(*) FROM news_oi_signals
                WHERE created_at_ms >= %s) AS telemetry_parsed_24h,
              (SELECT count(*) FROM news_verdicts
                WHERE stage = 'triage' AND error_code = 'oi_parse_failed'
                  AND created_at_ms >= %s) AS telemetry_parse_failed_24h,
              (SELECT count(*) FROM news_verdicts
                WHERE stage = 'triage' AND final_decision IN ('push','escalate')
                  AND created_at_ms >= %s
                  AND program_version = 'news_oi_signal_v1') AS telemetry_push_24h,
              -- #207: Events, not provider items. `telemetry_received_24h` counts exact OI source frames
              -- *before* the Gate, so it names frames the monitor's table can never show; the three by-rule
              -- buckets count judged verdicts, so together they miss a frame still waiting for one. This is
              -- the table's own universe — exactly the rows `admission=telemetry_deterministic&hours=24`
              -- serves — and it is what the 全部 tab counts.
              (SELECT count(*) FROM news_events
                WHERE opened_at_ms >= %s
                  AND admission = 'telemetry_deterministic') AS telemetry_events_24h
            """,
            (
                day_ago,
                OI_SOURCE_IDENTITY.strategy_id,
                OI_SOURCE_IDENTITY.strategy_name,
                OI_SOURCE_IDENTITY.source_type,
                OI_SOURCE_IDENTITY.engine_type,
                day_ago,
                day_ago,
                day_ago,
                day_ago,
            ),
        ).fetchone()
        return {key: int(value or 0) for key, value in dict(row or {}).items()}

    def _source_contracts_24h(self, *, day_ago: int) -> dict[str, dict[str, int]]:
        """One bounded Event cohort, projected into the five closed source-contract funnels."""

        rows = self.conn.execute(
            """
            SELECT e.event_kind, count(*) AS received,
                   count(*) FILTER (WHERE COALESCE(v.has_verdict, false)) AS verdict,
                   count(*) FILTER (
                     WHERE e.source_contract_reason IS NULL
                       AND NOT COALESCE(v.parse_failed, false)
                   ) AS parsed,
                   count(*) FILTER (
                     WHERE e.source_contract_reason = 'source_contract_drift'
                        OR COALESCE(v.parse_failed, false)
                   ) AS parse_failed
              FROM news_events e
              LEFT JOIN LATERAL (
                SELECT bool_or(true) AS has_verdict,
                       bool_or(error_code IN ('oi_parse_failed', 'liquidation_parse_failed')) AS parse_failed
                  FROM news_verdicts
                 WHERE event_id = e.event_id AND stage = 'triage'
              ) v ON true
             WHERE e.opened_at_ms >= %s
             GROUP BY e.event_kind
            """,
            (day_ago,),
        ).fetchall()
        by_kind = {str(row["event_kind"]): row for row in rows}
        result: dict[str, dict[str, int]] = {}
        for event_kind, family in zip(EVENT_KINDS, SOURCE_CONTRACT_FAMILIES, strict=True):
            row = by_kind.get(event_kind, {})
            received = int(row.get("received") or 0)
            parse_failed = int(row.get("parse_failed") or 0) if event_kind in {"oi", "liquidation"} else 0
            parsed = int(row.get("parsed") or 0) if event_kind in {"oi", "liquidation"} else received
            result[family] = {
                "received": received,
                "parsed": 0 if event_kind == "unsupported_market" else parsed,
                "parse_failed": parse_failed,
                "unsupported": received if event_kind == "unsupported_market" else 0,
                "verdict": int(row.get("verdict") or 0),
            }
        return result

    def _oi_by_rule_24h(self, *, day_ago: int) -> dict[str, int]:
        """The deterministic judge's own gate, counted over 24 h.

        `pipeline.dropped_by_rule` cannot answer this: it groups `override_rule`, and `decide()` writes
        `telemetry_deterministic` there for every OI verdict, push and withhold alike. The gate name lives
        only in the trace the judge wrote.

        Its own query, filtered to OI verdicts first, rather than another grouping key on the funnel's
        shared verdict scan. That scan reads `trace` for every Triage verdict in the window — 1948 rows and
        26 MB of JSONB on 2026-08-25 — and folding a second path extraction into it cost **+79 ms**
        (84 -> 164 ms), on an endpoint that already sits at ~730 ms against a 1 s statement timeout and is
        polled every 15 s by every open tab. `program_version` narrows the same answer to 94 rows and 147 kB:
        **1.2 ms**, identical counts. One cheap query beats one widened expensive one.
        """

        rows = self.conn.execute(
            """
            SELECT COALESCE(trace -> 'oi_signal' ->> 'rule', '') AS oi_rule, count(*) AS n
              FROM news_verdicts
             WHERE stage = 'triage' AND created_at_ms >= %s
               AND program_version = %s
             GROUP BY 1
            """,
            (day_ago, OI_PROGRAM_VERSION),
        ).fetchall()
        counts = {str(row["oi_rule"]): int(row["n"]) for row in rows if row["oi_rule"]}
        return dict(sorted(counts.items(), key=lambda kv: -kv[1]))

    def oi_window_occupancy(
        self,
        *,
        now_ms: int,
        window_ms: int,
        whale_oi_ratio_above_bps: int,
        oi_change_at_least_bps: int,
        limit: int = 40,
    ) -> list[dict[str, Any]]:
        """How many rank slots each symbol has already spent inside the live window.

        The eligibility predicate is `count_recent_eligible_oi_signals`' predicate verbatim, and for the same
        reason: an ineligible frame stays auditable but never spends a later signal's rank, so counting rows
        instead of filtering them would report a symbol as full that the next frame will sail straight past.
        The stored `rank_in_window` cannot answer this either — it is the rank that frame had when it landed,
        and a window that has since slid past its earlier siblings would keep reporting their high-water mark.

        Bounded by construction: `news_oi_signals` holds one row per parsed frame under a 30-day purge —
        roughly 190 a day, so low thousands at steady state. The window spans every symbol rather than one,
        so the planner takes the table rather than `ix_news_oi_signals_symbol_observed`; measured at 0.06 ms
        over 288 rows on 2026-08-25. Re-measure if the lane's daily volume or the retention window grows.
        """

        rows = self.conn.execute(
            """
            SELECT symbol, count(*)::int AS used
              FROM news_oi_signals
             WHERE metric_version = %s AND observed_at_ms > %s AND observed_at_ms <= %s
               AND whale_oi_ratio_bps > %s AND abs(oi_change_bps) >= %s
             GROUP BY symbol
             ORDER BY used DESC, symbol ASC
             LIMIT %s
            """,
            (
                OI_METRIC_VERSION,
                int(now_ms) - int(window_ms),
                int(now_ms),
                int(whale_oi_ratio_above_bps),
                int(oi_change_at_least_bps),
                int(limit),
            ),
        ).fetchall()
        return [{"symbol": str(row["symbol"]), "used": int(row["used"])} for row in rows]

    def status_snapshot(self, *, now_ms: int) -> dict[str, Any]:
        ingest = self.conn.execute("SELECT * FROM news_ingest_state WHERE singleton_key = 'opennews'").fetchone()
        incidents = self.open_incidents()  # type: ignore[attr-defined]
        day_ago = int(now_ms) - 24 * 3600_000
        hour_ago = int(now_ms) - 3600_000
        pipeline = self.conn.execute(
            """
            WITH event_counts AS (
              SELECT
                count(*) FILTER (WHERE opened_at_ms >= %s) AS events_1h,
                count(*) AS events_24h,
                count(*) FILTER (WHERE admission = 'candidate') AS candidates_24h
                FROM news_events
               WHERE opened_at_ms >= %s
            ), verdict_counts AS (
              SELECT
                -- Two denominators on purpose. The funnel is the reader's view — 收到 ⊇ 送审 ⊇ 模型判断
                -- ⊇ 决定推送 ⊇ 已送达, subtracted band by band by the console — and a telemetry judgment
                -- is a judgment and its push is a card the reader received, so both count here or the
                -- containment breaks at one end or the other. Model health is a different question and
                -- gets its own denominator below: ~190 arithmetic judgments a day, never degraded,
                -- would otherwise dilute the degraded share and make the model look healthier than it is.
                count(*) AS triage_24h,
                count(*) FILTER (
                  WHERE program_version IS DISTINCT FROM 'news_oi_signal_v1'
                ) AS model_triage_24h,
                count(*) FILTER (WHERE degraded) AS triage_degraded_24h,
                count(*) FILTER (WHERE final_decision IN ('push','escalate')) AS decided_push_24h,
                count(*) FILTER (WHERE final_decision = 'throttled') AS throttled_24h,
                -- #221: generated stored columns preserve the status values without decompressing the 26 MB
                -- daily TOAST corpus. One aggregate pass then replaces the old ten verdict scans.
                percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms)
                  FILTER (WHERE latency_ms IS NOT NULL) AS triage_p50_ms,
                percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms)
                  FILTER (WHERE latency_ms IS NOT NULL) AS triage_p95_ms,
                percentile_cont(0.95) WITHIN GROUP (ORDER BY queue_lag_ms)
                  FILTER (WHERE queue_lag_ms IS NOT NULL) AS queue_lag_p95_ms,
                count(*) FILTER (WHERE reasked_after_told_change) AS reasked_24h,
                count(*) FILTER (WHERE novelty_defaulted) AS novelty_defaulted_24h
                FROM news_verdicts
               WHERE stage = 'triage' AND created_at_ms >= %s
            )
            SELECT * FROM event_counts CROSS JOIN verdict_counts
            """,
            (hour_ago, day_ago, day_ago),
        ).fetchone()
        delivery = self.conn.execute(
            """
            SELECT
              (SELECT count(*) FROM news_deliveries WHERE state = 'sent' AND settled_at_ms >= %s) AS sent_24h,
              (SELECT count(*) FROM news_deliveries WHERE state = 'sent' AND settled_at_ms >= %s) AS sent_1h,
              (SELECT count(*) FROM news_deliveries WHERE state = 'terminal' AND settled_at_ms >= %s) AS terminal_24h,
              (SELECT error_code FROM news_deliveries WHERE state = 'terminal'
                ORDER BY settled_at_ms DESC NULLS LAST LIMIT 1) AS last_error_code,
              (SELECT percentile_cont(0.5)
                 WITHIN GROUP (ORDER BY (d.settled_at_ms - i.observed_at_ms)::double precision)
                 FROM news_deliveries d JOIN news_events e ON e.event_id = d.event_id
                 JOIN news_items i ON i.item_id = e.leader_item_id
                WHERE d.state = 'sent' AND d.kind = 'first' AND d.settled_at_ms >= %s) AS e2e_p50_ms,
              (SELECT percentile_cont(0.95)
                 WITHIN GROUP (ORDER BY (d.settled_at_ms - i.observed_at_ms)::double precision)
                 FROM news_deliveries d JOIN news_events e ON e.event_id = d.event_id
                 JOIN news_items i ON i.item_id = e.leader_item_id
                WHERE d.state = 'sent' AND d.kind = 'first' AND d.settled_at_ms >= %s) AS e2e_p95_ms
            """,
            (day_ago, hour_ago, day_ago, day_ago, day_ago),
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
                **self._oi_telemetry_24h(day_ago=day_ago),
                "source_classifier_version": SOURCE_CONTRACT_CLASSIFIER_VERSION,
                "source_contracts_24h": self._source_contracts_24h(day_ago=day_ago),
                **funnel,
            },
            "oi": {"by_rule_24h": self._oi_by_rule_24h(day_ago=day_ago)},
            "broker": dict(ingest["broker_snapshot"] or {}) if ingest else {},
            "delivery": {
                "sent_24h": int(delivery["sent_24h"] or 0) if delivery else 0,
                "sent_1h": int(delivery["sent_1h"] or 0) if delivery else 0,
                "terminal_24h": int(delivery["terminal_24h"] or 0) if delivery else 0,
                "last_error_code": delivery["last_error_code"] if delivery else None,
                "e2e_p50_ms": float(delivery["e2e_p50_ms"])
                if delivery and delivery["e2e_p50_ms"] is not None
                else None,
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

    def event_asset_symbols(self, event_ids: Sequence[str]) -> dict[str, list[str]]:
        """Event id -> durable asset symbols for one bounded public response (#287)."""

        wanted = list(dict.fromkeys(str(event_id) for event_id in event_ids if str(event_id)))
        if not wanted:
            return {}
        rows = self.conn.execute(
            """
            SELECT event_id, array_agg(symbol ORDER BY symbol) AS symbols
              FROM news_event_assets
             WHERE event_id = ANY(%s)
             GROUP BY event_id
            """,
            (wanted,),
        ).fetchall()
        return {str(row["event_id"]): [str(symbol) for symbol in row["symbols"] or []] for row in rows}

    def asset_usage_24h(self, *, now_ms: int) -> dict[str, list[str]]:
        """event_id -> durable Event-asset symbols for the last 24 h (#87/#267).

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
                   seen_scope,
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
        # The five Event-feed stages are one cohort, not five independent rolling windows. A verdict created
        # today for yesterday's Event still belongs in model-health throughput, but it must not make the
        # feed's 24 h funnel grow after the intake cohort has fallen out of the window. Every predicate below
        # therefore starts from the same set of Events opened in the window and asks how far each one got.
        totals = self.conn.execute(
            f"""
            SELECT count(*) AS events,
                   count(*) FILTER (WHERE admission IN ({_ADMITTED_SQL})) AS admitted,
                   count(*) FILTER (
                     WHERE admission IN ({_ADMITTED_SQL})
                       AND EXISTS (
                         SELECT 1 FROM news_verdicts v
                          WHERE v.event_id = news_events.event_id AND v.stage = 'triage'
                       )
                   ) AS triaged,
                   count(*) FILTER (
                     WHERE admission IN ({_ADMITTED_SQL})
                       AND EXISTS (
                         SELECT 1 FROM news_verdicts v
                          WHERE v.event_id = news_events.event_id AND v.stage = 'triage'
                       )
                       AND EXISTS (
                         SELECT 1 FROM news_deliveries d
                          WHERE d.event_id = news_events.event_id AND d.kind = 'first' AND d.state = 'sent'
                       )
                   ) AS delivered
              FROM news_events WHERE opened_at_ms >= %s
            """,
            (day_ago,),
        ).fetchone()
        events = int(totals["events"] or 0) if totals else 0
        admitted = int(totals["admitted"] or 0) if totals else 0
        triaged = int(totals["triaged"] or 0) if totals else 0
        delivered = int(totals["delivered"] or 0) if totals else 0
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
            "admitted_24h": admitted,
            # A material Event exists only after the transport/envelope parser succeeds, so this legacy
            # `parsed` equals the Event cohort. Strict source-contract parser outcomes live in
            # `source_contracts_24h`; provider frames are never an alternate fact table.
            "funnel_received_24h": events,
            "funnel_parsed_24h": events,
            "funnel_admitted_24h": admitted,
            "funnel_triaged_24h": triaged,
            "funnel_delivered_24h": delivered,
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
        "event_kind": card["event_kind"],
        "source_contract_reason": card.get("source_contract_reason"),
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


def _oi_summary(value: Any) -> dict[str, Any] | None:
    """The deterministic OI judgment behind one `telemetry_deterministic` row, or None for every other row.

    Every field is `oi_judgment_trace()` / `oi_parse_failure()` output read back verbatim — the browser is not
    allowed to re-run `oi_signal_parser_v1` over `leader_title`, and a number it re-derived would be a second
    parser to keep in step with the judge. The two thresholds come from the trace's own `policy`, so a stored
    frame keeps saying what it ran under after an operator retunes `news.oi`.

    `symbol` is the judge's parsed subject, not something the browser may infer from the wire title. The Gate
    grounds no provider tag for strategy 1019, so `grounded_assets` stays empty; after judgment the primary is
    durably attached through `news_event_assets` and projected separately as public `assets`. The feed's slim
    triage summary still omits its full `assets`, so this trace remains the authority for the OI subject and
    measurements. `direction` is not folded here — the slim triage summary
    does carry that one.

    `strategy_id`, `provider` and `provider_source` stay behind: the console does not display provider
    Strategy IDs.
    """

    if not isinstance(value, Mapping):
        return None
    policy = value.get("policy")
    policy = policy if isinstance(policy, Mapping) else {}
    return {
        "parsed": bool(value.get("parsed")),
        "rule": str(value.get("rule") or ""),
        "symbol": str(value.get("symbol") or "") or None,
        "oi_change_bps": _optional_int(value.get("oi_change_bps")),
        "oi_value_usd": _optional_int(value.get("oi_value_usd")),
        "whale_long_profit_bps": _optional_int(value.get("whale_long_profit_bps")),
        "whale_oi_ratio_bps": _optional_int(value.get("whale_oi_ratio_bps")),
        "eligible_rank_in_window": _optional_int(value.get("eligible_rank_in_window")),
        "rank_semantics": str(value.get("rank_semantics") or "") or None,
        "window_ms": _optional_int(policy.get("window_ms")),
        "max_rank_in_window": _optional_int(policy.get("max_rank_in_window")),
        "whale_oi_ratio_above_bps": _optional_int(policy.get("whale_oi_ratio_above_bps")),
        "oi_change_at_least_bps": _optional_int(policy.get("oi_change_at_least_bps")),
        "parser_version": str(value.get("parser_version") or "") or None,
        "failure_stage": str(value.get("failure_stage") or "") or None,
        "title_sha256": str(value.get("title_sha256") or "") or None,
    }


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
        "oi": _oi_summary(row.get("oi_signal")),
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
