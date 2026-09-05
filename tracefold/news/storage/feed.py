"""Bounded feed, detail, status, and public projection reads."""

from __future__ import annotations

import base64
import time
from collections.abc import Mapping, Sequence
from typing import Any

from ..models import ReaderReceipt
from ..outcome import (
    audience_zh,
    decision_zh,
    direction_zh,
    event_outcome,
    magnitude_zh,
    novelty_zh,
    scope_zh,
)
from ..search import NewsSearchPlan
from ..source_contracts import (
    EVENT_KINDS,
    EVENT_SOURCE_CONTRACT_FAMILIES,
    SOURCE_CONTRACT_CLASSIFIER_VERSION,
    EventKind,
)
from ..taxonomy import taxonomy_public
from ..timeline import event_timeline
from .feed_sql import (
    ASSET_SEARCH_PREDICATE,
    EDITORIAL_EVENT_SQL,
    EVENT_VERDICTS_SQL,
    OUTCOME_GROUP_SQL,
    STATUS_DELIVERY_SQL,
    STATUS_FUNNEL_REVIEWS_SQL,
    STATUS_FUNNEL_SUPPRESSED_SQL,
    STATUS_FUNNEL_TOTALS_SQL,
    STATUS_FUNNEL_VERDICTS_SQL,
    STATUS_INGEST_SQL,
    STATUS_LEARNING_RETENTION_SQL,
    STATUS_PIPELINE_SQL,
    STATUS_SOURCE_CONTRACTS_SQL,
    TEXT_SEARCH_PREDICATE,
    feed_counts_sql,
    feed_page_sql,
)


class FeedStorage:
    conn: Any

    def list_feed(
        self,
        *,
        event_family: tuple[str, ...] | None,
        change_state: tuple[str, ...] | None,
        assertion_status: tuple[str, ...] | None,
        source_authority: tuple[str, ...] | None,
        subject_code: tuple[str, ...] | None,
        final_decision: tuple[str, ...] | None,
        event_kind: tuple[EventKind, ...] | None,
        admission: str | None,
        search: NewsSearchPlan | None,
        limit: int,
        cursor: str | None,
        outcome: str | None = None,
        hours: int | None = None,
        directions: tuple[str, ...] | None = None,
        now_ms: int | None = None,
    ) -> dict[str, Any]:
        cursor_opened, cursor_id = _decode_cursor(cursor)
        handoff_now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
        # `where` / `params` accumulate the predicates every outcome group shares — the window and the reader's
        # filters. The outcome group itself and the cursor are appended after `_feed_counts` has taken a copy,
        # so the tab counts describe the whole filtered set rather than the page being served.
        params: list[Any] = []
        where = ["e.ingest_mode IN ('live', 'recovery')", EDITORIAL_EVENT_SQL]
        window_hours = int(hours) if hours else None
        if window_hours:
            # The response echoes `hours`, never the wall-clock bound, so an unchanged page keeps its ETag.
            since_ms = handoff_now_ms - window_hours * 3600_000
            where.append("e.opened_at_ms >= %s")
            params.append(since_ms)
        taxonomy_filters = (
            (event_family, "event_family"),
            (change_state, "change_state"),
            (assertion_status, "assertion_status"),
            (source_authority, "source_authority"),
        )
        for values, key in taxonomy_filters:
            if values:
                where.append(f"t.editorial #>> '{{taxonomy,{key}}}' = ANY(%s)")
                params.append(list(values))
        if subject_code:
            where.append("COALESCE(t.editorial #> '{taxonomy,subject_codes}', '[]'::jsonb) ?| %s")
            params.append(list(subject_code))
        if admission:
            where.append("e.admission = %s")
            params.append(admission)
        if search is not None:
            if search.mode == "asset":
                where.append(ASSET_SEARCH_PREDICATE)
                params.append(list(search.event_symbols))
            else:
                where.append(TEXT_SEARCH_PREDICATE)
                params.append(search.normalized_query)
        if final_decision:
            where.append("t.final_decision = ANY(%s)")
            params.append(list(final_decision))
        if directions:
            where.append("t.direction = ANY(%s)")
            params.append(list(directions))
        if event_kind and len(event_kind) < len(EVENT_KINDS):
            where.append("e.event_kind = ANY(%s)")
            params.append(list(event_kind))
        # Counting is worth one extra aggregate only on the first page; later pages reuse what it returned.
        # Snapshot the clauses so the outcome group and cursor appended below cannot reach the count query.
        wants_counts = cursor_opened is None
        counts = (
            self._feed_counts(where=list(where), params=list(params), now_ms=handoff_now_ms) if wants_counts else None
        )
        if outcome in OUTCOME_GROUP_SQL:
            where.append(OUTCOME_GROUP_SQL[outcome])
        if cursor_opened is not None:
            where.append("(e.opened_at_ms, e.event_id) < (%s, %s)")
            params.extend([cursor_opened, cursor_id])
        rows = self.conn.execute(
            feed_page_sql(" AND ".join(where)),
            (handoff_now_ms, *params, int(limit) + 1),
        ).fetchall()
        items = [_feed_row(dict(r), now_ms=handoff_now_ms) for r in rows[: int(limit)]]
        next_cursor = None
        if len(rows) > int(limit):
            last = rows[int(limit) - 1]
            next_cursor = _encode_cursor(int(last["opened_at_ms"]), str(last["event_id"]))
        return {
            "events": items,
            "next_cursor": next_cursor,
            "counts": counts,
            "filters": {
                "event_family": _joined_filter(event_family),
                "change_state": _joined_filter(change_state),
                "assertion_status": _joined_filter(assertion_status),
                "source_authority": _joined_filter(source_authority),
                "subject_code": _joined_filter(subject_code),
                "final_decision": _joined_filter(final_decision),
                "event_kind": _joined_filter(event_kind),
                "admission": admission,
                "symbol": search.symbol if search is not None else None,
                "q": search.q if search is not None else None,
                "limit": int(limit),
                "outcome": outcome if outcome in OUTCOME_GROUP_SQL else None,
                "hours": window_hours,
                "direction": ",".join(directions) if directions else None,
            },
            "search": search.public_metadata() if search is not None else None,
        }

    def _feed_counts(self, *, where: list[str], params: list[Any], now_ms: int) -> dict[str, int]:
        """How the reader's current filter splits across the three outcome groups.

        The three predicates partition the feed exactly (see `OUTCOME_GROUP_SQL`), so one pass with FILTER
        aggregates answers all four tabs. The joins mirror the feed query so a row counts here if and only if
        it would be served there, but the lateral takes only the column the predicates read rather than the
        whole verdict row.

        This is an unbounded aggregate on a three-second poll: it costs one pass over the filtered set, which
        is the last 24 h by default but the whole retention when the reader picks `hours=all`. Measured at
        19 ms over the entire table at ~2k Events / 1.3 days of retention; re-measure before letting either
        grow much, and cap the window here if it stops being free.
        """
        row = self.conn.execute(
            feed_counts_sql(" AND ".join(where)),
            (int(now_ms), *params),
        ).fetchone()
        return {key: int((row or {}).get(key) or 0) for key in ("total", "pushed", "held", "pending")}

    def event_detail(self, event_id: str) -> dict[str, Any] | None:
        # A retired market Event is immutable history, not a 404 waiting to be a 500: the public
        # `EventKind` cannot spell its kind, so the read refuses it here rather than handing the row to
        # a response envelope that will reject it (#553). Its observation is at `/api/news/market`.
        card = self._editorial_event_card(event_id)  # type: ignore[attr-defined]
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
        verdicts = self.conn.execute(EVENT_VERDICTS_SQL, (event_id,)).fetchall()
        deliveries = self.conn.execute(
            """
            SELECT kind, state, card, receipt, error_code, attempted_at_ms, settled_at_ms,
                   created_at_ms, edit_state, pending_card, edit_error_code,
                   edit_attempted_at_ms, edit_settled_at_ms
              FROM news_deliveries
             WHERE event_id = %s
             ORDER BY created_at_ms
            """,
            (event_id,),
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
        timeline_verdict_rows = [
            dict(r) | {"model_editorial": dict(r["editorial"]) if r["editorial"] is not None else None}
            for r in verdicts
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
            event=event,
            members=member_rows,
            verdicts=timeline_verdict_rows,
            deliveries=delivery_rows,
            now_ms=int(time.time() * 1000),
        )
        latest_triage = next((dict(v) for v in reversed(verdicts) if v["stage"] == "triage"), None)
        latest_editorial = dict((latest_triage or {}).get("editorial") or {})
        return {
            "event": event,
            "outcome": outcome.as_dict(),
            "triage": _triage_summary(
                final_decision=(latest_triage or {}).get("final_decision"),
                override_rule=(latest_triage or {}).get("override_rule"),
                throttled_by=(latest_triage or {}).get("throttled_by"),
                degraded=(latest_triage or {}).get("degraded"),
                error_code=(latest_triage or {}).get("error_code"),
                verdict=(latest_triage or {}).get("verdict") or {},
                taxonomy=latest_editorial.get("taxonomy"),
                relevance=latest_editorial.get("relevance"),
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
                    "created_at_ms": int(row["created_at_ms"]),
                }
                for row in self.conn.execute(
                    """
                    SELECT event_id, evidence_version, focus_fact_id, evidence_sha256,
                           provenance, release_eligible, created_at_ms
                      FROM news_event_evidence_snapshots
                     WHERE event_id = %s AND provenance = 'observed'
                       AND snapshot ->> 'schema_version' = 'news_event_evidence_v3'
                     ORDER BY evidence_version
                    """,
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
            SELECT j.review_id, j.subject_kind, j.event_id, j.external_snapshot_id,
                   j.should_push, j.first_bad_owner, j.evidence_refs, j.expected_correction,
                   j.note, j.reviewer, j.created_at_ms, j.rubric_version,
                   j.reader_contract_version, j.pairwise_case_id, counts.judgment_n
              FROM (
                SELECT count(*) AS judgment_n FROM news_review_records_v1
                 WHERE event_id = %s AND review_kind = 'judgment'
                   AND subject_kind = 'event'
              ) counts
              LEFT JOIN LATERAL (
                SELECT judgment.review_id, judgment.subject_kind, judgment.event_id,
                       judgment.external_snapshot_id, judgment.should_push,
                       judgment.first_bad_owner, judgment.evidence_refs,
                       judgment.expected_correction, judgment.note, judgment.reviewer,
                       judgment.created_at_ms, judgment.rubric_version,
                       judgment.reader_contract_version, judgment.pairwise_case_id
                  FROM news_review_records_v1 acceptance
                  JOIN news_review_records_v1 judgment ON judgment.review_id = acceptance.accepts_review_id
                 WHERE acceptance.review_kind = 'acceptance' AND judgment.event_id = %s
                   AND judgment.subject_kind = 'event'
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
                "subject_kind": row["subject_kind"],
                "event_id": row["event_id"],
                "external_snapshot_id": row["external_snapshot_id"],
                "should_push": row["should_push"],
                "first_bad_owner": row["first_bad_owner"],
                "evidence_refs": list(row["evidence_refs"] or []),
                "expected_correction": row["expected_correction"],
                "note": row["note"],
                "reviewer": row["reviewer"],
                "created_at_ms": int(row["created_at_ms"]),
                "rubric_version": row["rubric_version"],
                "reader_contract_version": row["reader_contract_version"],
                "pairwise_case_id": row["pairwise_case_id"],
            }
        return {
            "judgment_n": int(row["judgment_n"] or 0),
            "accepted": accepted,
            "uncertain": bool(accepted and accepted["should_push"] == "uncertain"),
        }

    def _source_contracts_24h(self, *, day_ago: int) -> dict[str, dict[str, int]]:
        """One bounded Event cohort, projected into the two editorial source-contract funnels.

        Market sources left this counter with the Events they no longer create (#553). Their intake is
        a market question and `market_sources` answers it from the facts themselves, so a reader who
        wants to know what 1019 sent in the last day is not asking the editorial funnel to guess.
        """

        rows = self.conn.execute(
            STATUS_SOURCE_CONTRACTS_SQL,
            (day_ago,),
        ).fetchall()
        by_kind = {str(row["event_kind"]): row for row in rows}
        result: dict[str, dict[str, int]] = {}
        for event_kind, family in zip(EVENT_KINDS, EVENT_SOURCE_CONTRACT_FAMILIES, strict=True):
            row = by_kind.get(event_kind, {})
            received = int(row.get("received") or 0)
            result[family] = {
                "received": received,
                "parsed": received,
                "verdict": int(row.get("verdict") or 0),
            }
        return result

    def status_snapshot(self, *, now_ms: int) -> dict[str, Any]:
        ingest = self.conn.execute(STATUS_INGEST_SQL).fetchone()
        incidents = self.open_incidents()  # type: ignore[attr-defined]
        recovery = self.recovery_backlog()  # type: ignore[attr-defined]
        day_ago = int(now_ms) - 24 * 3600_000
        hour_ago = int(now_ms) - 3600_000
        pipeline = self.conn.execute(
            STATUS_PIPELINE_SQL,
            (hour_ago, day_ago, day_ago),
        ).fetchone()
        delivery = self.conn.execute(
            STATUS_DELIVERY_SQL,
            (day_ago, hour_ago, day_ago, day_ago, day_ago),
        ).fetchone()
        funnel = self._funnel_24h(day_ago=day_ago)
        retention = self.conn.execute(STATUS_LEARNING_RETENTION_SQL).fetchone()
        return {
            "ingest": {
                "connected": bool(ingest["connected"]) if ingest else False,
                "last_frame_at_ms": ingest["last_frame_at_ms"] if ingest else None,
                "last_publish_at_ms": ingest["last_publish_at_ms"] if ingest else None,
                "last_error_code": ingest["last_error_code"] if ingest else None,
                "recovery": recovery,
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
                "source_classifier_version": SOURCE_CONTRACT_CLASSIFIER_VERSION,
                "source_contracts_24h": self._source_contracts_24h(day_ago=day_ago),
                **funnel,
            },
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
            SELECT asset.event_id, array_agg(asset.symbol ORDER BY asset.symbol) AS symbols
              FROM news_event_assets asset
              JOIN news_events current_event ON current_event.event_id = asset.event_id
             WHERE asset.event_id = ANY(%s)
             GROUP BY asset.event_id
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
            SELECT asset.event_id, array_agg(asset.symbol ORDER BY asset.symbol) AS symbols
              FROM news_event_assets asset
              JOIN news_events current_event ON current_event.event_id = asset.event_id
             WHERE asset.opened_at_ms >= %s
             GROUP BY asset.event_id
            """,
            (int(now_ms) - 24 * 3600_000,),
        ).fetchall()
        return {str(row["event_id"]): [str(s) for s in (row["symbols"] or [])] for row in rows}

    def _funnel_24h(self, *, day_ago: int) -> dict[str, Any]:
        """Where the last 24 h of Events went, by named reason: Gate admissions, decide() rules, storyline keys."""

        suppressed = self.conn.execute(
            STATUS_FUNNEL_SUPPRESSED_SQL,
            (day_ago,),
        ).fetchall()
        # One pass over the last 24 h of Triage verdicts; the four named maps are folded from it in Python.
        verdict_groups = self.conn.execute(
            STATUS_FUNNEL_VERDICTS_SQL,
            (day_ago,),
        ).fetchall()
        dropped: dict[str, int] = {}
        throttled: dict[str, int] = {}
        pushed_by_rule: dict[str, int] = {}
        degraded_by_code: dict[str, int] = {}
        # Current duplicate withholds name the exact sent-ledger measurement scope.
        duplicates: dict[str, int] = {"all": 0}
        for row in verdict_groups:
            n = int(row["n"])
            final = str(row["final_decision"])
            if final == "drop":
                dropped[str(row["rule"])] = dropped.get(str(row["rule"]), 0) + n
            elif final == "throttled":
                throttled[str(row["key"])] = throttled.get(str(row["key"]), 0) + n
                if str(row["key"]).endswith(":seen"):
                    duplicates["all"] += n
            elif final in {"push", "escalate"}:
                pushed_by_rule[str(row["rule"])] = pushed_by_rule.get(str(row["rule"]), 0) + n
            if row["degraded"]:
                degraded_by_code[str(row["code"])] = degraded_by_code.get(str(row["code"]), 0) + n
        # Both current Review shapes of "the reader should have got this": an accepted Event judgment and an
        # accepted ExternalMissSnapshot. The latter is the only observed upper bound on upstream recall.
        # Release eligibility and the active epoch are material facts; genesis removed old review contracts.
        missed = self.conn.execute(
            STATUS_FUNNEL_REVIEWS_SQL,
            (day_ago,),
        ).fetchone()
        # The four Event-feed stages are one cohort, not four independent rolling windows. A verdict created
        # today for yesterday's Event still belongs in model-health throughput, but it must not make the
        # feed's 24 h funnel grow after the intake cohort has fallen out of the window. Every predicate below
        # therefore starts from the same set of Events opened in the window and asks how far each one got.
        totals = self.conn.execute(
            STATUS_FUNNEL_TOTALS_SQL,
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
            "funnel_received_24h": events,
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
        "event_kind": card["event_kind"],
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
    verdict: Mapping[str, Any] | None = None,
    taxonomy: Mapping[str, Any] | None = None,
    relevance: Mapping[str, Any] | None = None,
    full: bool = False,
) -> dict[str, Any] | None:
    """The reader-facing Triage summary shared by the feed row and the Event detail.

    Every business word is resolved to Chinese here so no browser owns a vocabulary table (the Feishu card in
    ``delivery.py`` emits the same `DIRECTION_ZH`/`MAGNITUDE_ZH` words, and one definition keeps the card and
    the console from drifting); the raw enum ships beside it purely so the UI can pick a visual tone.

    ``full`` is the Event detail. The feed row renders only direction/magnitude over 25 rows, so it takes
    the slim shape — carrying the detail fields there cost 20.7% of the feed payload for nothing."""

    if not final_decision:
        return None
    v: Mapping[str, Any] = verdict or {}
    direction = v.get("direction")
    magnitude = _optional_int(v.get("magnitude"))
    scope = v.get("scope")
    summary = {
        "final_decision": final_decision,
        "override_rule": override_rule,
        "throttled_by": throttled_by,
        "degraded": bool(degraded),
        "error_code": error_code,
        "direction": direction,
        "magnitude": magnitude,
        "headline_zh": v.get("headline_zh"),
        "direction_zh": direction_zh(direction),
        "magnitude_zh": magnitude_zh(magnitude),
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
        "taxonomy": taxonomy_public(taxonomy) if taxonomy is not None else None,
        "relevance": dict(relevance) if relevance is not None else None,
        "why_zh": v.get("why_zh"),
        "assets": _triage_assets(v.get("assets")),
        "scope_zh": scope_zh(scope),
        "novelty_zh": novelty_zh(novelty),
        "audience_zh": audience_zh(audience),
        "decision_zh": decision_zh(final_decision),
    }


def _triage_assets(value: Any) -> list[dict[str, str | None]]:
    if not isinstance(value, list):
        return []
    out: list[dict[str, str | None]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        symbol = str(item["symbol"]).strip()
        if not symbol:
            continue
        market_type = item["market_type"]
        out.append(
            {
                "symbol": symbol,
                "market_type": None if market_type is None else str(market_type),
                "role": str(item["role"]),
            }
        )
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


def _feed_row(row: Mapping[str, Any], *, now_ms: int) -> dict[str, Any]:
    triage = _triage_summary(
        final_decision=row.get("final_decision"),
        override_rule=row.get("override_rule"),
        throttled_by=row.get("throttled_by"),
        degraded=row.get("triage_degraded"),
        error_code=row.get("triage_error_code"),
        verdict=row.get("triage_verdict") or {},
        taxonomy=dict(row.get("model_editorial") or {}).get("taxonomy"),
        relevance=dict(row.get("model_editorial") or {}).get("relevance"),
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
    outcome_triage = (
        {
            **triage,
            "created_at_ms": row.get("verdict_created_at_ms"),
            "published_at_ms": row.get("verdict_published_at_ms"),
        }
        if triage is not None
        else None
    )
    outcome = event_outcome(
        admission=row.get("admission"),
        opened_at_ms=row.get("opened_at_ms"),
        published_at_ms=row.get("published_at_ms"),
        triage=outcome_triage,
        delivery=delivery,
        now_ms=now_ms,
    )
    return {
        **_event_public(row),
        "outcome": outcome.as_dict(),
        "triage": triage,
        "delivery": delivery,
    }


def _verdict_public(row: Mapping[str, Any]) -> dict[str, Any]:
    editorial = row.get("editorial")
    model_editorial = None
    if isinstance(editorial, Mapping):
        taxonomy = editorial.get("taxonomy")
        relevance = editorial.get("relevance")
        if isinstance(taxonomy, Mapping) and isinstance(relevance, Mapping):
            model_editorial = {
                "taxonomy": taxonomy_public(taxonomy),
                "relevance": dict(relevance),
            }
    return {
        "stage": row["stage"],
        "policy_version": row["policy_version"],
        "judgment_contract_version": row["judgment_contract_version"],
        "judgment_origin": row["judgment_origin"],
        "judgment_sha256": row["scored_judgment_sha256"],
        "verdict": dict(row.get("verdict") or {}),
        "model_editorial": model_editorial,
        "rule_baseline_decision": row["rule_baseline_decision"],
        "final_decision": row["final_decision"],
        "override_rule": row.get("override_rule"),
        "throttled_by": row.get("throttled_by"),
        "model": row.get("model"),
        "program_version": row.get("program_version"),
        "program_sha256": row.get("program_sha256"),
        "degraded": bool(row.get("degraded")),
        "error_code": row.get("error_code"),
        "evidence_version": row.get("evidence_version"),
        "evidence_sha256": row.get("evidence_sha256"),
        "focus_fact_id": row.get("focus_fact_id"),
        "published_at_ms": row.get("published_at_ms"),
        "created_at_ms": int(row["created_at_ms"]),
    }


def _joined_filter(values: Sequence[str] | None) -> str | None:
    return ",".join(values) if values else None
