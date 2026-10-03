"""Bounded feed, detail, status, and public projection reads."""

from __future__ import annotations

import base64
import time
from collections.abc import Mapping, Sequence
from typing import Any

from ..models import MARKET_TYPES, ReaderReceipt, market_type_of
from ..outcome import event_outcome
from ..search import NewsSearchPlan
from ..source_contracts import (
    EVENT_KINDS,
    EVENT_SOURCE_CONTRACT_FAMILIES,
    SOURCE_CONTRACT_CLASSIFIER_VERSION,
    EventKind,
)
from ..timeline import event_timeline, reader_delivery
from ..update_view import (
    UPDATE_DECODE_ERROR,
    decode_update,
    effective_notification,
    event_update_view,
    headline_claim_ref,
    intent_views,
    notification_view,
    previous_content_refs,
    semantic_view,
    sent_headline,
)
from ..updates.contracts import NOTIFICATION_CHANGES
from . import update_reads
from .collectors import STATUS_INGEST_SQL
from .feed_sql import (
    ASSET_SEARCH_PREDICATE,
    EDITORIAL_EVENT_SQL,
    EVENT_MEMBERS_SQL,
    ITEM_RELATED_COUNT_SQL,
    ITEM_RELATED_EVENTS_SQL,
    ITEM_RELATED_KEYS_SQL,
    OUTCOME_GROUP_SQL,
    SOURCE_AUTHORITY_PREDICATE,
    STATUS_DELIVERY_SQL,
    STATUS_FUNNEL_DECISIONS_SQL,
    STATUS_FUNNEL_TOTALS_SQL,
    STATUS_PIPELINE_SQL,
    STATUS_PRIMARY_ASSET_MARKETS_SQL,
    STATUS_SOURCE_CONTRACTS_SQL,
    SUBJECT_CODE_PREDICATE,
    TEXT_SEARCH_PREDICATE,
    feed_counts_sql,
    feed_page_sql,
)

EVENT_STORY_SQL = feed_page_sql(
    "e.storyline_key=%s AND e.opened_at_ms BETWEEN %s AND %s AND " + EDITORIAL_EVENT_SQL,
    order_sql="(e.event_id=%s) DESC, e.opened_at_ms DESC, e.event_id DESC",
)
STORY_HALF_WINDOW_MS = 24 * 3600_000
STORY_EVENT_LIMIT = 30


class FeedStorage:
    conn: Any

    def list_feed(
        self,
        *,
        source_authority: tuple[str, ...] | None,
        subject_code: tuple[str, ...] | None,
        event_kind: tuple[EventKind, ...] | None,
        admission: str | None,
        search: NewsSearchPlan | None,
        limit: int,
        cursor: str | None,
        outcome: str | None = None,
        hours: int | None = None,
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
            where.append(SOURCE_AUTHORITY_PREDICATE)
            params.append(list(source_authority))
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
        if event_kind and len(event_kind) < len(EVENT_KINDS):
            where.append("e.event_kind = ANY(%s)")
            params.append(list(event_kind))
        # First-page counts and rows share one PostgreSQL statement snapshot. Later pages need no count.
        wants_counts = cursor_opened is None
        count_where = " AND ".join(where)
        count_params = tuple(params)
        if outcome in OUTCOME_GROUP_SQL:
            where.append(OUTCOME_GROUP_SQL[outcome])
        if cursor_opened is not None:
            where.append("(e.opened_at_ms, e.event_id) < (%s, %s)")
            params.extend([cursor_opened, cursor_id])
        page_sql = feed_page_sql(" AND ".join(where))
        if wants_counts:
            statement = (
                f"WITH page AS MATERIALIZED ({page_sql}), counts AS MATERIALIZED ({feed_counts_sql(count_where)}) "  # noqa: S608
                "SELECT page.*, counts.total AS feed_total, counts.pushed AS feed_pushed, "
                "counts.held AS feed_held, counts.pending AS feed_pending "
                "FROM counts LEFT JOIN page ON true "
                "ORDER BY page.opened_at_ms DESC NULLS LAST, page.event_id DESC NULLS LAST"
            )
            result = self.conn.execute(statement, (*params, int(limit) + 1, *count_params)).fetchall()
            count_row = result[0]
            counts = {key: int(count_row[f"feed_{key}"] or 0) for key in ("total", "pushed", "held", "pending")}
            rows = [row for row in result if row["event_id"] is not None]
        else:
            rows = self.conn.execute(page_sql, (*params, int(limit) + 1)).fetchall()
            counts = None
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
                "event_kind": _joined_filter(event_kind),
                "admission": admission,
                "symbol": search.symbol if search is not None else None,
                "q": search.q if search is not None else None,
                "limit": int(limit),
                "outcome": outcome if outcome in OUTCOME_GROUP_SQL else None,
                "hours": window_hours,
            },
            "search": search.public_metadata() if search is not None else None,
        }

    def item_related_events(self, *, item_id: str, after_event_id: str | None, limit: int) -> dict[str, Any]:
        """Page every Event this Item contributed to, including non-leader membership."""

        total = self.conn.execute(ITEM_RELATED_COUNT_SQL, (item_id,)).fetchone()
        keys = self.conn.execute(
            ITEM_RELATED_KEYS_SQL,
            (item_id, after_event_id, after_event_id, limit + 1),
        ).fetchall()
        page_ids = [str(row["event_id"]) for row in keys[:limit]]
        rows = (
            self.conn.execute(
                ITEM_RELATED_EVENTS_SQL,
                (item_id, item_id, page_ids),
            ).fetchall()
            if page_ids
            else []
        )
        return {
            "item_id": item_id,
            "total_events": int(total["n"] if total else 0),
            "events": [
                {
                    "event_id": str(row["event_id"]),
                    "leader_item_id": str(row["leader_item_id"]),
                    "member_scopes": list(row["member_scopes"] or []),
                    "match_kinds": list(row["match_kinds"] or []),
                    "focus_fact_text": str(row["focus_fact_text"]),
                    "focus_fact_method": str(row["focus_fact_method"]),
                    "wanted_revision": row["wanted_revision"],
                    "done_revision": row["done_revision"],
                    "semantic_outcome": row["last_outcome"],
                    "semantic_error_code": row["last_error_code"],
                    "adopted_content_revision": row["adopted_content_revision"],
                    "notification_state": row["notification_state"],
                    "notification_action": row["notification_action"],
                    "intent_state": row["intent_state"],
                    "sent_count": int(row["sent_count"]),
                }
                for row in rows
            ],
            "next_cursor": page_ids[-1] if len(keys) > limit else None,
        }

    def event_detail(self, event_id: str) -> dict[str, Any] | None:
        card = self._editorial_event_card(event_id)  # type: ignore[attr-defined]
        if card is None:
            return None
        members = self.conn.execute(
            EVENT_MEMBERS_SQL,
            (event_id,),
        ).fetchall()
        deliveries = update_reads.event_deliveries(self.conn, event_id)
        queue = update_reads.event_delivery_queue(self.conn, event_id)
        # Durable EventUpdate work and actual reader receipts remain separate facts.
        work = update_reads.semantic_work(self.conn, event_id)
        head = update_reads.event_update_head(self.conn, event_id)
        on_update_path = work is not None or head is not None
        revisions = update_reads.event_update_revisions(self.conn, event_id) if on_update_path else []
        observations = update_reads.semantic_observations(self.conn, event_id) if on_update_path else []
        notification = update_reads.notification_work(self.conn, event_id) if on_update_path else None
        effective = effective_notification(head, notification)
        decoded = decode_update(head["document"]) if head is not None else None
        head_info = (
            {
                "content_revision": head["content_revision"],
                "has_notification_changes": any(change.kind in NOTIFICATION_CHANGES for change in decoded.changes)
                if decoded is not None
                else None,
            }
            if head is not None
            else None
        )
        duplicates = update_reads.duplicate_claims(self.conn, event_id) if head is not None else []
        snapshots = [
            {
                "event_id": row["event_id"],
                "evidence_version": int(row["evidence_version"]),
                "focus_fact_id": row["focus_fact_id"],
                "evidence_sha256": row["evidence_sha256"],
                "provenance": "observed",
                "release_eligible": True,
                "created_at_ms": int(row["created_at_ms"]),
            }
            for row in self.conn.execute(
                """
                SELECT e.event_id,v.evidence_version,v.focus_fact_id,v.evidence_sha256,
                       v.created_at_ms FROM news_events e
                CROSS JOIN LATERAL jsonb_to_recordset(e.evidence->'versions') AS v(
                  evidence_version integer,focus_fact_id text,evidence_sha256 text,created_at_ms bigint)
                WHERE e.event_id=%s ORDER BY v.evidence_version
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
        delivery_rows = [_delivery_public(row) for row in deliveries]
        intents = intent_views(queue, deliveries)
        event_update = None
        update_error_code = None
        if head is not None:
            prior = update_reads.previous_claims(self.conn, event_id, previous_content_refs(head["document"]))
            current_intents = [
                intent
                for intent in intents
                if intent.get("content_revision")
                in {
                    head["content_revision"],
                    (effective or {}).get("content_revision")
                    if (effective or {}).get("carried")
                    else head["content_revision"],
                }
            ]
            event_update = event_update_view(head, previous_claims=prior, sent_headline=sent_headline(current_intents))
            if event_update is not None:
                event_update["duplicates"] = duplicates
                by_ref = {row["claim_ref"]: row for row in duplicates}
                for change in event_update["changes"]:
                    if change["kind"] == "restatement":
                        change["original"] = by_ref.get(change["current_ref"])
            update_error_code = UPDATE_DECODE_ERROR if event_update is None else None
            # Once an adopted head exists its current primary assets own the reader projection. An
            # empty or undecodable head never falls back to unrelated provider tags.
            event["assets"] = [
                asset
                for claim in (event_update or {}).get("claims", [])
                if not claim["retired"] and not claim["superseded"]
                for asset in claim["assets"]
                if asset["role"] == "primary"
            ]
        statements = {str(claim["ref"]): str(claim["statement"]) for claim in (event_update or {}).get("claims", [])}
        notification_public = notification_view(effective, statements=statements)
        update_reads.attach_earlier_receipts(self.conn, effective, notification_public)
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
        reader_card = reader_delivery(deliveries, None if head is None else str(head["content_revision"]))
        outcome, timeline = event_timeline(
            event=event,
            members=member_rows,
            deliveries=deliveries,
            delivery_queue=_owed_intent(queue, deliveries),
            semantic=work,
            adopted=head is not None,
            notification=_notification_outcome_input(effective or notification),
            head=head_info,
            duplicate=duplicates[0] if duplicates else None,
            has_restatement=bool(duplicates),
            headline_ref=headline_claim_ref(decoded) if decoded is not None else None,
            evidence_snapshots=snapshots if on_update_path else [],
            revisions=revisions,
            observations=observations,
            notification_view=notification_public,
            intents=intents,
        )
        return {
            "event": event,
            "outcome": outcome.as_dict(),
            "event_update": event_update,
            "processing": processing,
            "timeline": timeline,
            "members": member_rows,
            "deliveries": delivery_rows,
            "evidence_snapshots": snapshots,
            "reader_receipt": ReaderReceipt.from_delivery(
                _delivery_public(reader_card) if reader_card is not None else None
            ).model_dump(mode="json"),
            "story": self.event_story(event),
        }

    def event_story(self, event: Mapping[str, Any]) -> dict[str, Any] | None:
        key = str(event.get("storyline_key") or "")
        if key in {"", "none"}:
            return None
        center = int(event["opened_at_ms"])
        start, end = center - STORY_HALF_WINDOW_MS, center + STORY_HALF_WINDOW_MS
        raw = self.conn.execute(
            EVENT_STORY_SQL, (key, start, end, event["event_id"], STORY_EVENT_LIMIT + 1, event["event_id"])
        ).fetchall()
        rows = [_feed_row(row, now_ms=center) for row in raw[:STORY_EVENT_LIMIT]]
        return {
            "storyline_key": key,
            "from_ms": start,
            "to_ms": end,
            "has_more": len(raw) > STORY_EVENT_LIMIT,
            "events": [
                {
                    "event_id": row["event_id"],
                    "headline": (row.get("update") or {}).get("headline") or row["leader_title"],
                    "reporting_origin": row["reporting_origin"],
                    "published_at_ms": row["published_at_ms"],
                    "opened_at_ms": row["opened_at_ms"],
                    "outcome": row["outcome"],
                    "received_at_ms": (row.get("delivery") or {}).get("settled_at_ms")
                    if (row.get("delivery") or {}).get("state") == "sent"
                    else None,
                }
                for row in sorted(rows, key=lambda row: (row["opened_at_ms"], row["event_id"]))
            ],
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
                "adopted": int(row.get("adopted") or 0),
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
            "primary_asset_markets_24h": self.primary_asset_markets_24h(now_ms=now_ms),
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
        }

    def primary_asset_markets_24h(self, *, now_ms: int) -> dict[str, Any]:
        """Primary asset occurrences in adopted semantic understandings completed in the last day.

        Count every adopted extraction, including earlier revisions, rather than current heads or
        notification receipts. Empty samples have no unknown share; this observation sets no gate.
        """
        rows = self.conn.execute(
            STATUS_PRIMARY_ASSET_MARKETS_SQL, (int(now_ms) - 24 * 3600_000, int(now_ms))
        ).fetchall()
        by_market = dict.fromkeys(MARKET_TYPES, 0)
        for row in rows:
            by_market[market_type_of(row["market_type"])] += int(row["n"])
        total = sum(by_market.values())
        unknown = by_market["unknown"]
        return {
            "total": total,
            "unknown": unknown,
            "unknown_share": round(unknown / total, 4) if total else None,
            "by_market": by_market,
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
        """One Event cohort and current notification decisions with distinct reviewer facts."""
        decisions = self.conn.execute(STATUS_FUNNEL_DECISIONS_SQL, (day_ago,)).fetchall()
        totals = self.conn.execute(STATUS_FUNNEL_TOTALS_SQL, (day_ago,)).fetchone()
        events = int(totals["events"] or 0) if totals else 0
        admitted = int(totals["admitted"] or 0) if totals else 0
        return {
            "decision_actions_24h": {str(row["action"]): int(row["n"]) for row in decisions},
            "candidate_share_24h": round(admitted / events, 4) if events else None,
            "admitted_24h": admitted,
            "funnel_received_24h": events,
            "funnel_admitted_24h": admitted,
            "funnel_adopted_24h": int(totals["adopted"] or 0) if totals else 0,
            "funnel_delivered_24h": int(totals["delivered"] or 0) if totals else 0,
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


def _feed_row(row: Mapping[str, Any], *, now_ms: int) -> dict[str, Any]:
    delivery = (
        {
            "state": row["delivery_state"],
            "settled_at_ms": row.get("delivered_at_ms"),
            "error_code": row.get("delivery_error_code"),
            "content_revision": row.get("delivery_content_revision"),
            "payload_sha256": row.get("delivery_payload_sha256"),
        }
        if row.get("delivery_state")
        else None
    )
    original = row.get("duplicate_info")
    outcome = event_outcome(
        admission=row.get("admission"),
        delivery=delivery | {"plan_key": row.get("delivery_plan_key")} if delivery is not None else None,
        delivery_queue={
            "state": row.get("delivery_queue_state"),
            "error_code": row.get("delivery_queue_error_code"),
            "content_revision": row.get("delivery_queue_content_revision"),
            "frozen_card": row.get("delivery_queue_frozen"),
        },
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
                "attempts": row.get("notification_attempts"),
                "last_error_code": row.get("notification_last_error_code"),
                "content_revision": row.get("notification_content_revision"),
                "action": row.get("notification_action"),
                "claim_decisions": row.get("notification_claim_decisions"),
                "decided_at_ms": row.get("notification_decided_at_ms"),
                "added_sources": row.get("notification_added_sources"),
            }
            if row.get("notification_state")
            else None
        ),
        head={
            "content_revision": row.get("update_content_revision"),
            "has_notification_changes": row.get("head_has_notification_changes"),
        },
        duplicate=update_reads.duplicate_view(original) if original else None,
        has_restatement=original is not None,
        headline_ref=row.get("update_headline_claim_ref"),
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
        **({"assets": list(row.get("update_primary_assets") or [])} if update is not None else {}),
        "outcome": outcome.as_dict(),
        "update": update,
        "delivery": delivery,
    }


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
    owed = [row for row in queue if str(row["intent_id"]) not in settled]
    if not owed:
        return None
    return max(owed, key=lambda row: (int(row.get("enqueued_at_ms") or 0), str(row["intent_id"])))


def _notification_outcome_input(work: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if work is None:
        return None
    stored = work.get("plan")
    plan: Mapping[str, Any] = stored if isinstance(stored, Mapping) else {}
    return {
        **dict(work),
        "state": work["state"],
        "attempts": work.get("attempts"),
        "last_error_code": work.get("last_error_code"),
        "content_revision": work.get("content_revision"),
        "action": plan.get("action"),
        "claim_decisions": plan.get("claim_decisions"),
    }


def _joined_filter(values: Sequence[str] | None) -> str | None:
    return ",".join(values) if values else None
