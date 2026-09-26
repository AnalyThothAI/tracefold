"""Bounded feed, detail, status, and public projection reads."""

from __future__ import annotations

import base64
import time
from collections.abc import Mapping, Sequence
from typing import Any

from ..evidence import execution_evidence_views
from ..models import ReaderReceipt, market_type_of
from ..outcome import (
    decision_zh,
    direction_zh,
    event_outcome,
    fact_kind_zh,
    novelty_zh,
    scope_zh,
)
from ..row_values import optional_float
from ..search import NewsSearchPlan
from ..source_contracts import (
    EVENT_KINDS,
    EVENT_SOURCE_CONTRACT_FAMILIES,
    SOURCE_CONTRACT_CLASSIFIER_VERSION,
    EventKind,
)
from ..taxonomy import source_authority_zh
from ..timeline import event_timeline, reader_delivery
from ..update_view import (
    UPDATE_DECODE_ERROR,
    event_update_view,
    intent_views,
    notification_view,
    previous_content_refs,
    semantic_view,
    sent_headline,
)
from . import update_reads
from .decisions import editorial_read_shape, triage_verdict_read_shape
from .feed_sql import (
    ASSET_SEARCH_PREDICATE,
    EDITORIAL_EVENT_SQL,
    EVENT_MEMBERS_SQL,
    EVENT_VERDICTS_SQL,
    OUTCOME_GROUP_SQL,
    SOURCE_AUTHORITY_PREDICATE,
    STATUS_DELIVERY_SQL,
    STATUS_FUNNEL_REVIEW_RATIOS_SQL,
    STATUS_FUNNEL_REVIEWS_SQL,
    STATUS_FUNNEL_SUPPRESSED_SQL,
    STATUS_FUNNEL_TOTALS_SQL,
    STATUS_FUNNEL_VERDICTS_SQL,
    STATUS_INGEST_SQL,
    STATUS_LEARNING_RETENTION_SQL,
    STATUS_PIPELINE_SQL,
    STATUS_SOURCE_CONTRACTS_SQL,
    SUBJECT_CODE_PREDICATE,
    TEXT_SEARCH_PREDICATE,
    feed_counts_sql,
    feed_page_sql,
)


class FeedStorage:
    conn: Any

    def list_feed(
        self,
        *,
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
        if source_authority:
            # #706: the authority of the sources an adopted update cites, else the legacy editorial one.
            where.append(SOURCE_AUTHORITY_PREDICATE)
            params.extend([list(source_authority), list(source_authority)])
        if subject_code:
            where.append(SUBJECT_CODE_PREDICATE)
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
            EVENT_MEMBERS_SQL,
            (event_id,),
        ).fetchall()
        verdicts = self.conn.execute(EVENT_VERDICTS_SQL, (event_id,)).fetchall()
        deliveries = update_reads.event_deliveries(self.conn, event_id)
        queue = update_reads.event_delivery_queue(self.conn, event_id)
        # #706: the EventUpdate plane. A legacy Event has none of these rows and reads exactly as before.
        work = update_reads.semantic_work(self.conn, event_id)
        head = update_reads.event_update_head(self.conn, event_id)
        on_update_path = work is not None or head is not None
        revisions = update_reads.event_update_revisions(self.conn, event_id) if on_update_path else []
        observations = update_reads.semantic_observations(self.conn, event_id) if on_update_path else []
        notification = update_reads.notification_work(self.conn, event_id) if on_update_path else None
        snapshots = [
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
        ]
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
        timeline_verdict_rows = [dict(r) | {"model_editorial": editorial_read_shape(r["editorial"])} for r in verdicts]
        verdict_rows = [_verdict_public(dict(r)) for r in verdicts]
        delivery_rows = [_delivery_public(row) for row in deliveries]
        intents = intent_views(queue, deliveries)
        event_update = None
        update_error_code = None
        if head is not None:
            prior = update_reads.previous_claims(self.conn, event_id, previous_content_refs(head["document"]))
            event_update = event_update_view(head, previous_claims=prior, sent_headline=sent_headline(intents))
            update_error_code = UPDATE_DECODE_ERROR if event_update is None else None
        statements = {str(claim["ref"]): str(claim["statement"]) for claim in (event_update or {}).get("claims", [])}
        notification_public = notification_view(notification, statements=statements)
        processing = (
            {
                "semantic": semantic_view(work),
                "observations": [
                    {
                        "result_id": row["result_id"],
                        "input_revision": int(row["input_revision"]),
                        "program_identity": row["program_identity"],
                        "completed_at_ms": int(row["completed_at_ms"]),
                        "adopted_content_revision": row["adopted_content_revision"],
                    }
                    for row in observations
                ],
                "notification": notification_public,
                "intents": intents,
                "update_error_code": update_error_code,
            }
            if on_update_path or intents
            else None
        )
        reader_card = reader_delivery(deliveries)
        outcome, timeline = event_timeline(
            event=event,
            members=member_rows,
            verdicts=timeline_verdict_rows,
            deliveries=deliveries,
            delivery_queue=_owed_intent(queue, deliveries),
            semantic=work,
            adopted=head is not None,
            notification=_notification_outcome_input(notification),
            evidence_snapshots=snapshots if on_update_path else [],
            revisions=revisions,
            observations=observations,
            notification_view=notification_public,
            intents=intents,
            now_ms=int(time.time() * 1000),
        )
        latest_triage = next((dict(v) for v in reversed(verdicts) if v["stage"] == "triage"), None)
        latest_editorial = editorial_read_shape((latest_triage or {}).get("editorial"))
        evidence_inputs = execution_evidence_views(verdicts)
        selected_inputs = [entry for entry in evidence_inputs if entry["selected"]]
        last_cutoff = max((entry["cutoff_at_ms"] for entry in selected_inputs), default=None)
        late_evidence = []
        if last_cutoff is not None:
            late_evidence = [
                dict(row)
                for row in self.conn.execute(
                    """
                SELECT item_id AS material_id, 'provider_payload'::text AS material_kind,
                       provider_params_available_at_ms AS available_at_ms
                  FROM news_items WHERE item_id=%s AND provider_params_available_at_ms > %s
                ORDER BY available_at_ms DESC LIMIT 8
                """,
                    (card["leader_item_id"], last_cutoff),
                ).fetchall()
            ]

        return {
            "event": event,
            "outcome": outcome.as_dict(),
            "event_update": event_update,
            "processing": processing,
            # History only: the Triage verdict an Event was judged by before #706. A new Event has none,
            # and nothing here is merged into `event_update`.
            "legacy_verdict": _legacy_verdict(
                final_decision=(latest_triage or {}).get("final_decision"),
                override_rule=(latest_triage or {}).get("override_rule"),
                throttled_by=(latest_triage or {}).get("throttled_by"),
                degraded=(latest_triage or {}).get("degraded"),
                error_code=(latest_triage or {}).get("error_code"),
                verdict=(latest_triage or {}).get("verdict") or {},
                editorial=latest_editorial,
                full=True,
            ),
            "timeline": timeline,
            "members": member_rows,
            "verdicts": verdict_rows,
            "deliveries": delivery_rows,
            "review": self._review_summary(event_id),
            "evidence_inputs": evidence_inputs,
            "late_evidence": late_evidence,
            "evidence_snapshots": snapshots,
            "reader_receipt": ReaderReceipt.from_delivery(
                _delivery_public(reader_card) if reader_card is not None else None
            ).model_dump(mode="json"),
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
        # Release eligibility is a material fact of the review; genesis removed old review contracts.
        missed = self.conn.execute(
            STATUS_FUNNEL_REVIEWS_SQL,
            (day_ago,),
        ).fetchone()
        # #675 §4. The daily audit's two product ratios, over accepted judgments rather than cards: how much
        # of what the reader got a reviewer would keep, and how much of what was withheld they would have
        # sent. Both carry their numerator and denominator so a two-review day reads as a two-review day.
        ratios = self.conn.execute(
            STATUS_FUNNEL_REVIEW_RATIOS_SQL,
            (day_ago,),
        ).fetchone()
        sent_n = int(ratios["sent_n"] or 0) if ratios else 0
        sent_push_n = int(ratios["sent_push_n"] or 0) if ratios else 0
        dropped_n = int(ratios["dropped_n"] or 0) if ratios else 0
        dropped_push_n = int(ratios["dropped_push_n"] or 0) if ratios else 0
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
            "keep_ratio_sent_24h": {
                "ratio": round(sent_push_n / sent_n, 4) if sent_n else None,
                "numerator": sent_push_n,
                "denominator": sent_n,
            },
            "missed_ratio_dropped_24h": {
                "ratio": round(dropped_push_n / dropped_n, 4) if dropped_n else None,
                "numerator": dropped_push_n,
                "denominator": dropped_n,
            },
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


def _legacy_verdict(
    *,
    final_decision: Any,
    override_rule: Any = None,
    throttled_by: Any = None,
    degraded: Any = False,
    error_code: Any = None,
    verdict: Mapping[str, Any] | None = None,
    editorial: Mapping[str, Any] | None = None,
    full: bool = False,
) -> dict[str, Any] | None:
    """The reader-facing summary of one legacy Triage verdict, shared by the feed row and the Event detail.

    History since #706: `news_verdicts` receives no writes, and an Event judged by the News Agent has no
    verdict and therefore no summary. Every business word is resolved to Chinese here so no browser owns a
    vocabulary table; the raw enum ships beside it purely so the UI can pick a visual tone.

    ``full`` is the Event detail. The feed row renders only direction/fact kind over 25 rows, so it takes
    the slim shape — carrying the detail fields there cost 20.7% of the feed payload for nothing.

    A verdict written under `news_judgment_v2` carries `magnitude` and `audience` and no `fact_kind`;
    those rows are audit truth and are never rewritten, so `fact_kind` reads as ``None`` for them and
    the badge is simply absent (#675 §1). The retired taxonomy axes are not summarized: the verdict rows
    keep their stored values for audit, and nothing projects them into a current reading (#706)."""

    if not final_decision:
        return None
    v: Mapping[str, Any] = verdict or {}
    direction = v.get("direction")
    fact_kind = v.get("fact_kind")
    scope = v.get("scope")
    summary = {
        "final_decision": final_decision,
        "override_rule": override_rule,
        "throttled_by": throttled_by,
        "degraded": bool(degraded),
        "error_code": error_code,
        "direction": direction,
        "fact_kind": fact_kind,
        "headline_zh": v.get("headline_zh"),
        "direction_zh": direction_zh(direction),
        "fact_kind_zh": fact_kind_zh(fact_kind),
    }
    if not full:
        return summary
    novelty = v.get("novelty")
    # The read shape `editorial_read_shape` produces, or nothing at all for a degraded/OI/liquidation
    # verdict that has no editorial sibling. `source_authority` is a code fact about the evidence.
    e: Mapping[str, Any] = editorial or {}
    return summary | {
        "scope": scope,
        "novelty": novelty,
        "evidence_ref": v.get("evidence_ref"),
        "confidence": optional_float(v.get("confidence")),
        "source_authority": e.get("source_authority"),
        "source_authority_zh": source_authority_zh(e.get("source_authority")),
        "why_zh": v.get("why_zh"),
        "assets": _triage_assets(v.get("assets")),
        "scope_zh": scope_zh(scope),
        "novelty_zh": novelty_zh(novelty),
        "decision_zh": decision_zh(final_decision),
    }


def _triage_assets(value: Any) -> list[dict[str, str]]:
    """The stored verdict's assets, typed. A pre-#651 free string or `null` reads as `unknown`."""

    if not isinstance(value, list):
        return []
    out: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        symbol = str(item["symbol"]).strip()
        if not symbol:
            continue
        out.append(
            {
                "symbol": symbol,
                "market_type": market_type_of(item.get("market_type")),
                "role": str(item["role"]),
            }
        )
    return out


def _feed_row(row: Mapping[str, Any], *, now_ms: int) -> dict[str, Any]:
    legacy = _legacy_verdict(
        final_decision=row.get("final_decision"),
        override_rule=row.get("override_rule"),
        throttled_by=row.get("throttled_by"),
        degraded=row.get("triage_degraded"),
        error_code=row.get("triage_error_code"),
        verdict=row.get("triage_verdict") or {},
        editorial=editorial_read_shape(row.get("model_editorial")),
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
            **legacy,
            "created_at_ms": row.get("verdict_created_at_ms"),
            "published_at_ms": row.get("verdict_published_at_ms"),
        }
        if legacy is not None
        else None
    )
    outcome = event_outcome(
        admission=row.get("admission"),
        opened_at_ms=row.get("opened_at_ms"),
        published_at_ms=row.get("published_at_ms"),
        triage=outcome_triage,
        delivery=delivery | {"plan_key": row.get("delivery_plan_key")} if delivery is not None else None,
        delivery_queue={"state": row.get("delivery_queue_state"), "error_code": row.get("delivery_queue_error_code")},
        semantic=(
            {
                "wanted_revision": row.get("semantic_wanted_revision"),
                "done_revision": row.get("semantic_done_revision"),
                "last_outcome": row.get("semantic_last_outcome"),
                "last_error_code": row.get("semantic_last_error_code"),
            }
            if row.get("has_semantic_work")
            else None
        ),
        adopted=row.get("update_content_revision") is not None,
        notification=(
            {
                "state": row["notification_state"],
                "action": row.get("notification_action"),
                "claim_decisions": row.get("notification_claim_decisions"),
            }
            if row.get("notification_state")
            else None
        ),
        now_ms=now_ms,
    )
    sent_update = row.get("sent_update_headline")
    headline = sent_update or row.get("update_claim_headline")
    update = (
        {
            "content_revision": row["update_content_revision"],
            "adopted_at_ms": int(row["update_adopted_at_ms"]),
            "claim_n": int(row.get("update_claim_n") or 0),
            "headline": headline,
            "headline_source": "sent_card" if sent_update else ("claim" if headline else None),
        }
        if row.get("update_content_revision") is not None
        else None
    )
    return {
        **_event_public(row),
        "outcome": outcome.as_dict(),
        "update": update,
        "legacy_verdict": legacy,
        "delivery": delivery,
    }


def _verdict_public(row: Mapping[str, Any]) -> dict[str, Any]:
    trace = row.get("trace")
    trace = trace if isinstance(trace, dict) else {}
    editorial = editorial_read_shape(row.get("editorial"))
    model_editorial = None
    if editorial is not None:
        taxonomy = editorial["taxonomy"]
        model_editorial = {
            "source_authority": editorial["source_authority"],
            "source_authority_zh": source_authority_zh(editorial["source_authority"]),
            "taxonomy": _legacy_taxonomy(taxonomy) if taxonomy is not None else None,
            "taxonomy_status": editorial["taxonomy_status"],
            "taxonomy_error_code": editorial["taxonomy_error_code"],
        }
    return {
        "stage": row["stage"],
        "policy_version": row["policy_version"],
        "judgment_contract_version": row["judgment_contract_version"],
        "judgment_origin": row["judgment_origin"],
        "judgment_sha256": row["scored_judgment_sha256"],
        "verdict": triage_verdict_read_shape(row.get("verdict")),
        "model_editorial": model_editorial,
        "rule_baseline_decision": row["rule_baseline_decision"],
        "final_decision": row["final_decision"],
        "override_rule": row.get("override_rule"),
        "throttled_by": row.get("throttled_by"),
        "model": row.get("model"),
        "model_usage_coverage": trace.get("usage_coverage", "unknown"),
        "model_input_tokens": trace.get("input_tokens") if trace.get("usage_coverage") == "complete" else None,
        "model_output_tokens": trace.get("output_tokens") if trace.get("usage_coverage") == "complete" else None,
        "model_provider_cost_microusd": (trace.get("provider_cost_microusd") if "usage_coverage" in trace else None),
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


def _legacy_taxonomy(value: Mapping[str, Any]) -> dict[str, Any]:
    """The four retired taxonomy axes as the legacy verdict stored them, for audit and nothing else.

    No vocabulary is applied: the axes have no current owner or reading since #706, so the stored codes
    are published as stored, and a missing axis stays missing.
    """

    codes = value.get("subject_codes")
    return {
        "subject_codes": [str(code) for code in codes] if isinstance(codes, list) else [],
        "event_family": _optional_text(value.get("event_family")),
        "change_state": _optional_text(value.get("change_state")),
        "assertion_status": _optional_text(value.get("assertion_status")),
    }


def _optional_text(value: Any) -> str | None:
    return str(value) if value else None


def _delivery_public(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "intent_id": row["intent_id"],
        "kind": row["kind"],
        "state": row["state"],
        "error_code": row["error_code"],
        "attempted_at_ms": int(row["attempted_at_ms"]),
        "settled_at_ms": row["settled_at_ms"],
        "card": dict(row["card"] or {}),
        "pending_card": dict(row["pending_card"]) if row["pending_card"] is not None else None,
        "receipt": row["receipt"],
        "edit_state": row["edit_state"],
        "edit_error_code": row["edit_error_code"],
        "edit_attempted_at_ms": row["edit_attempted_at_ms"],
        "edit_settled_at_ms": row["edit_settled_at_ms"],
    }


def _owed_intent(queue: Sequence[Mapping[str, Any]], rows: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    """The Event's latest reader intent still owed with no ledger row -- `q` in the feed statement."""

    settled = {str(row["intent_id"]) for row in rows}
    owed = [row for row in queue if row.get("kind") in {"first", "update"} and str(row["intent_id"]) not in settled]
    if not owed:
        return None
    return max(owed, key=lambda row: (int(row.get("enqueued_at_ms") or 0), str(row["intent_id"])))


def _notification_outcome_input(work: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if work is None:
        return None
    stored = work.get("plan")
    plan: Mapping[str, Any] = stored if isinstance(stored, Mapping) else {}
    return {
        "state": work["state"],
        "action": plan.get("action"),
        "claim_decisions": plan.get("claim_decisions"),
    }


def _joined_filter(values: Sequence[str] | None) -> str | None:
    return ",".join(values) if values else None
