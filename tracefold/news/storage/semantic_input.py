"""Frozen local source material and same-source read targets on the caller connection.

Commands use the caller's existing transaction; no external I/O or independent commit.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Final

from ..events.grounding import commodity_context_present
from ..market_review.instruments import normalize_symbol
from ..models import market_type_of
from ..taxonomy import source_authority
from ..updates.contracts import (
    EstablishedRelation,
    EventUpdate,
    Evidence,
    FrozenInput,
    PriorClaim,
    ReadTarget,
    Source,
    SourceAssetCandidate,
    SourceAssetTag,
)
from ..updates.identity import identity
from ..updates.projection import ReadingView, extraction_scopes, item_text, reading_view
from .errors import EventUpdateConflict
from .evidence import EvidenceStorage
from .semantic_jobs import semantic_job

READ_TARGETS_MAX: Final = 4


READ_TARGET_PREFIX: Final = "news_item:"


def item_evidence(item: Mapping[str, Any]) -> Evidence | None:
    """One stored provider Item as model-visible evidence with code-owned provenance.

    The source clocks are the Item's first observation and provider publication; the authority is the
    News source-authority classifier over the reporting origin and URL, never a model value.
    """

    text = item_text(item)
    if not text:
        return None
    origin = str(item.get("reporting_origin") or "").strip() or None
    url = str(item.get("canonical_url") or "").strip() or None
    artifact_id = str(item.get("source_artifact_id") or "").strip() or str(item["source_item_key"])
    return Evidence.issue(
        text,
        Source(
            publisher_id=str(item["source_id"]),
            artifact_id=artifact_id,
            artifact_revision=str(item.get("evidence_text_sha256") or "") or "1",
            record_id=str(item["item_id"]),
            origin_id=origin,
            published_at_ms=None if item.get("published_at_ms") is None else int(item["published_at_ms"]),
            first_available_at_ms=(
                min(int(item["observed_at_ms"]), int(item["published_at_ms"]))
                if item.get("first_ingest_mode") == "recovery" and item.get("published_at_ms") is not None
                else int(item["observed_at_ms"])
            ),
            url=url,
            source_authority=source_authority(tuple(value for value in (origin, url) if value)),
        ),
    )


def revision_evidence(item: Mapping[str, Any], revision: Mapping[str, Any]) -> Evidence | None:
    """A later source/body version of one provider record, with its own provenance and clock.

    The first body stays evidence too; a correction or an added exemption is visible only beside the
    text it revised.
    """

    text = str(revision.get("evidence_text") or "").strip()
    first = item_evidence(item)
    if not text or first is None:
        return None
    origin = str(revision.get("reporting_origin") or "").strip() or None
    url = str(revision.get("canonical_url") or "").strip() or None
    return Evidence.issue(
        text,
        first.source.model_copy(
            update={
                "artifact_revision": str(revision["revision_sha256"]),
                "revision_sequence": int(revision.get("revision_sequence") or 0),
                "artifact_id": str(revision.get("source_artifact_id") or "").strip() or str(item["source_item_key"]),
                "origin_id": origin,
                "url": url,
                "published_at_ms": int(revision["published_at_ms"]),
                "first_available_at_ms": int(revision["received_at_ms"]),
                "source_authority": source_authority(tuple(value for value in (origin, url) if value)),
            }
        ),
    )


def read_target_ref(item_id: str) -> str:
    return f"{READ_TARGET_PREFIX}{item_id}"


def read_target_item_id(ref: str) -> str | None:
    value = ref.removeprefix(READ_TARGET_PREFIX)
    return value if ref.startswith(READ_TARGET_PREFIX) and value else None


def _provider_metadata(material: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    """Provider tags remain attached to their own stored Item or frozen member snapshot."""

    metadata = {str(row["item_id"]): row.get("provider_metadata") or {} for row in material.get("items") or ()}
    card = material.get("card") or {}
    if card.get("leader_item_id") and card.get("provider_metadata"):
        metadata[str(card["leader_item_id"])] = card["provider_metadata"]
    metadata.update(
        {
            str(member["item_id"]): member["provider_metadata"]
            for member in material.get("members") or ()
            if member.get("provider_metadata")
        }
    )
    return metadata


def _source_asset_tags(
    material: Mapping[str, Any], evidence: Sequence[Evidence]
) -> dict[str, tuple[SourceAssetTag, ...]]:
    metadata = _provider_metadata(material)
    candidates = {}
    for item in evidence:
        rows = []
        for coin in metadata.get(item.source.record_id or "", {}).get("coins") or ():
            if not isinstance(coin, Mapping) or not isinstance(coin.get("symbol"), str) or not coin["symbol"].strip():
                continue
            # A grade is source context, never an admission rule. Preserve the provider spelling;
            # legacy forex is an explicit synonym, while fund alone establishes no market class.
            rows.append(
                SourceAssetTag(
                    symbol=coin["symbol"],
                    market_type=market_type_of(coin.get("market_type")),
                    grade=None if coin.get("grade") is None else str(coin["grade"]),
                )
            )
        if rows:
            candidates[item.ref] = tuple(rows)
    return candidates


def _source_asset_candidates(
    material: Mapping[str, Any], evidence: Sequence[Evidence], tags: Mapping[str, tuple[SourceAssetTag, ...]]
) -> dict[str, tuple[SourceAssetCandidate, ...]]:
    listed = material.get("listed_markets") or {}
    return {
        item.ref: tuple(
            SourceAssetCandidate(**tag.model_dump(), listed_markets=listed.get(normalize_symbol(tag.symbol), ()))
            for tag in tags[item.ref]
            if commodity_context_present(normalize_symbol(tag.symbol), item.text)
        )
        for item in evidence
        if item.ref in tags
    }


_VisibleMaterial = tuple[str, tuple[tuple[int, int, str], ...]]


def _visible_material(row: Evidence, view: ReadingView) -> _VisibleMaterial:
    """What extraction is shown of one source: its body and this Event's reading range over it."""

    return row.text, tuple((span.start, span.end, span.role) for span in view.spans)


def _unread(complete: Sequence[Evidence], views: Sequence[ReadingView], completed: set[str]) -> tuple[Evidence, ...]:
    """Pending task reads, one per visible material, in snapshot order.

    A copy, whether another provider record or a revision that changed only provenance, is no new input once
    identical material was read (or failed) or is already pending ahead of it: verbatim copies are not
    independent confirmation, and source asset candidates do not tell copies apart. A record's own body change
    is always read, a return to one of its earlier bodies included.
    """

    seen = {
        _visible_material(row, view) for row, view in zip(complete, views, strict=True) if view.read_ref in completed
    }
    previous: dict[str, _VisibleMaterial] = {}
    unread = []
    for row, view in zip(complete, views, strict=True):
        key = _visible_material(row, view)
        record = row.source.record_id
        changed = record is not None and previous.get(record, key) != key
        if record is not None:
            previous[record] = key
        if view.read_ref in completed or (key in seen and not changed):
            continue
        seen.add(key)
        unread.append(row)
    return tuple(unread)


def frozen_input(event_id: str, material: Mapping[str, Any]) -> FrozenInput:
    """Assemble the frozen semantic input from one consistent read.

    Evidence contains task reads not yet recorded for this Event: newly joined members, changed task
    scopes, later bodies of an existing Item, or a bounded optional read. The complete snapshot is
    read consistently before comparing read refs. Prior claims are this Event's
    adopted head only. Foreign recall belongs to the post-extraction workflow.
    """

    work: Mapping[str, Any] | None = material.get("work")
    head_document = material.get("head")
    attached = work is not None and bool(work.get("attached_evidence"))
    if work is not None and attached:
        evidence = [Evidence.model_validate(value) for value in work["attached_evidence"]]
        focus = tuple(str(value) for value in work.get("focus_claim_refs") or ())
    else:
        items = {str(row["item_id"]): row for row in material.get("items") or ()}
        revisions: dict[str, list[Mapping[str, Any]]] = {}
        for row in material.get("revisions") or ():
            revisions.setdefault(str(row["item_id"]), []).append(row)
        evidence = []
        for item_id in material.get("item_ids") or ():
            item = items.get(str(item_id))
            if item is None:
                continue
            value = item_evidence(item)
            if value is not None:
                evidence.append(value)
            for revision in sorted(
                revisions.get(str(item_id), ()),
                key=lambda row: (
                    int(row.get("revision_sequence") or 0),
                    int(row["received_at_ms"]),
                    row["revision_sha256"],
                ),
            ):
                revised = revision_evidence(item, revision)
                if revised is not None:
                    evidence.append(revised)
        focus = ()
    if not evidence:
        raise LookupError("news_event_input_missing")
    complete = tuple({row.ref: row for row in evidence}.values())
    all_tags = _source_asset_tags(material, complete)
    all_candidates = _source_asset_candidates(material, complete, all_tags)
    all_scopes = () if attached else extraction_scopes(material, complete)
    # A source ref proves only which body was stored, not which task boundary
    # was read.  Construct the current view before comparing completed reads.
    # A read that failed is settled too: it is quarantined until an exact reanalysis names it.
    completed = set((work or {}).get("processed_read_refs") or ()) | set((work or {}).get("failed_read_refs") or ())
    requested_read = (work or {}).get("reanalysis_read_ref")
    views = tuple(reading_view(event_id, row, all_scopes, all_tags.get(row.ref, ())) for row in complete)
    if requested_read is None:
        unique = _unread(complete, views, completed)
    else:
        # An exact reanalysis reads the one named view, even when identical material was read before.
        unique = tuple(row for row, view in zip(complete, views, strict=True) if view.read_ref == requested_read)
        if not unique:
            raise EventUpdateConflict("news_reanalysis_read_scope_changed")
    selected = {row.ref for row in unique}
    scopes = tuple(scope for scope in all_scopes if scope.evidence_ref in selected)
    prior: tuple[PriorClaim, ...] = ()
    if head_document is not None:
        head = EventUpdate.model_validate(head_document)
        prior = tuple(
            PriorClaim(event_id=head.event_id, content_revision=head.content_revision, claim=claim)
            for claim in head.current_claims
        )
    read_targets: tuple[ReadTarget, ...] = ()
    if not attached:
        read_targets = tuple(
            ReadTarget(
                ref=read_target_ref(str(row["item_id"])), action="load_prior_statement", description=str(row["title"])
            )
            for row in material.get("read_targets") or ()
            if str(row.get("title") or "").strip()
        )
    wanted = int(work["wanted_revision"]) if work is not None else max(1, int(material.get("evidence_version") or 1))
    lineage = str(work["lineage_id"]) if work is not None else identity("lineage", event_id, wanted)
    return FrozenInput(
        event_id=event_id,
        revision=wanted,
        lineage_id=lineage,
        evidence=unique,
        asset_candidates={ref: rows for ref, rows in all_candidates.items() if ref in selected},
        source_asset_tags={ref: rows for ref, rows in all_tags.items() if ref in selected},
        extraction_scopes=scopes,
        prior=prior,
        read_targets=read_targets,
        focus_claim_refs=focus,
        open_questions={}
        if head_document is None
        else {row.ref: row for row in EventUpdate.model_validate(head_document).open_questions},
        established_relations=tuple(
            EstablishedRelation.model_validate(row) for row in material.get("established_relations") or ()
        ),
        reanalysis_reason=None if work is None else work.get("reanalysis_reason"),
        reanalysis_head_ref=None if work is None else work.get("reanalysis_head_ref"),
    )


log = logging.getLogger("tracefold.news")


class SemanticInputStorage:
    def __init__(
        self,
        conn: Any,
        *,
        evidence: EvidenceStorage,
        head_document: Callable[[str], dict[str, Any] | None],
    ) -> None:
        self.conn = conn
        self.evidence = evidence
        self.head_document = head_document

    def semantic_input_material(self, event_id: str, *, now_ms: int) -> dict[str, Any]:
        work = semantic_job(
            self.conn.execute(
                "SELECT * FROM news_jobs WHERE job_kind='semantic' AND subject_id=%s", (event_id,)
            ).fetchone()
        )
        from .events import EventStorage

        events = EventStorage()
        events.conn = self.conn
        snapshot = events.latest_evidence_snapshot(event_id)
        state = self.conn.execute(
            "SELECT evidence->'fact_scopes' AS fact_scopes FROM news_events WHERE event_id=%s", (event_id,)
        ).fetchone()
        if snapshot is not None:
            snapshot["fact_scopes"] = state["fact_scopes"]
        document: dict[str, Any] = {} if snapshot is None else dict(snapshot["snapshot"] or {})
        card = dict(document.get("card") or {})
        members = list(document.get("members") or ())
        item_ids = list(
            dict.fromkeys(
                str(value)
                for value in (card.get("leader_item_id"), *(member.get("item_id") for member in members))
                if value
            )
        )
        frozen_revisions = [
            (str(member["item_id"]), str(revision_sha))
            for member in members
            for revision_sha in member.get("evidence_revisions") or ()
        ]
        read_item_ids = list(
            dict.fromkeys(
                (
                    *item_ids,
                    *(row.get("source", {}).get("record_id") for row in (work or {}).get("attached_evidence") or ()),
                )
            )
        )
        read_item_ids = [value for value in read_item_ids if value]
        items = (
            self.conn.execute(
                """
                SELECT item_id, source_id, source_item_key, source_artifact_id, title, description,
                       canonical_url, reporting_origin, published_at_ms, observed_at_ms, first_ingest_mode,
                       evidence_text, evidence_text_sha256, provider_metadata
                  FROM news_items WHERE item_id = ANY(%s)
                """,
                (read_item_ids,),
            ).fetchall()
            if read_item_ids
            else []
        )
        revisions = (
            self.conn.execute(
                """
                SELECT i.item_id, r.revision_sha256, r.revision_sequence, r.evidence_text, r.reporting_origin,
                       r.source_artifact_id,
                       r.canonical_url, r.published_at_ms, r.received_at_ms
                  FROM unnest(%s::text[], %s::text[]) AS frozen(item_id,revision_sha256)
                  JOIN news_items i ON i.item_id=frozen.item_id
                  CROSS JOIN LATERAL jsonb_to_recordset(i.revisions) AS r(
                    revision_sha256 text,revision_sequence bigint,evidence_text text,reporting_origin text,
                    source_artifact_id text,canonical_url text,published_at_ms bigint,received_at_ms bigint)
                 WHERE r.revision_sha256=frozen.revision_sha256
                """,
                ([row[0] for row in frozen_revisions], [row[1] for row in frozen_revisions]),
            ).fetchall()
            if frozen_revisions
            else []
        )
        material = {
            "work": None if work is None else dict(work),
            "evidence_version": None if snapshot is None else int(snapshot["evidence_version"]),
            "item_ids": item_ids,
            "card": card,
            "members": members,
            "fact_scopes": {} if snapshot is None else dict(snapshot["fact_scopes"] or {}),
            "items": [dict(row) for row in items],
            "revisions": [dict(row) for row in revisions],
            "head": self.head_document(event_id),
            "established_relations": self._established_relations(event_id),
        }
        # One query bounded by this snapshot's provider symbols, before freezing model input.
        # Aliases name a canonical base; the reference directory and unclassified rows are no
        # evidence of the exchange-listed market that OpenNews's `cex` tag refers to.
        symbols = sorted(
            {
                normalize_symbol(coin["symbol"])
                for metadata in _provider_metadata(material).values()
                for coin in metadata.get("coins") or ()
                if isinstance(coin, Mapping) and isinstance(coin.get("symbol"), str) and coin["symbol"].strip()
            }
        )
        rows = (
            self.conn.execute(
                """
                SELECT requested.symbol, array_agg(DISTINCT i.instrument_class ORDER BY i.instrument_class) AS markets
                  FROM unnest(%s::text[]) AS requested(symbol)
                  LEFT JOIN news_symbol_aliases a ON a.alias = requested.symbol
                  JOIN news_market_instruments i ON i.base_symbol = COALESCE(a.base_symbol, requested.symbol)
                 WHERE i.status = 'trading' AND i.venue <> 'us.listed' AND i.instrument_class <> 'unknown'
                 GROUP BY requested.symbol
                """,
                (symbols,),
            ).fetchall()
            if symbols
            else ()
        )
        material["listed_markets"] = {str(row["symbol"]): tuple(row["markets"]) for row in rows}
        # Only same-source read targets belong to the claim snapshot. Foreign
        # proposition recall happens after extraction, in its own short read.
        material["read_targets"] = self._same_source_targets(items, exclude_item_ids=item_ids, now_ms=now_ms)
        return material

    def _established_relations(self, event_id: str) -> list[dict[str, str]]:
        """Corrections and conflicts this Event's adopted revisions already published, by claim pair."""

        rows = self.conn.execute(
            """
            SELECT DISTINCT change->>'current_ref' AS current_ref, change->>'previous_ref' AS previous_ref,
                   change->>'relation' AS relation
              FROM news_analyses u CROSS JOIN LATERAL jsonb_array_elements(u.document->'changes') change
             WHERE u.adopted_at_ms IS NOT NULL AND u.event_id = %s AND change->>'relation' IN ('corrects', 'conflicts')
             ORDER BY 1, 2, 3
            """,
            (event_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def _same_source_targets(
        self, items: Sequence[Mapping[str, Any]], *, exclude_item_ids: Sequence[str], now_ms: int
    ) -> list[dict[str, Any]]:
        artifacts = [str(r["source_artifact_id"]) for r in items if r.get("source_artifact_id")]
        publishers = [str(r["source_id"]) for r in items if r.get("source_artifact_id")]
        urls = [str(r["canonical_url"]) for r in items if r.get("canonical_url")]
        return [
            dict(r)
            for r in self.conn.execute(
                """SELECT item_id,title FROM news_items
                WHERE item_id <> ALL(%s::text[]) AND observed_at_ms <= %s
                  AND ((source_id,source_artifact_id) IN
                       (SELECT * FROM unnest(%s::text[],%s::text[])) OR canonical_url=ANY(%s::text[]))
                ORDER BY observed_at_ms DESC,item_id LIMIT %s""",
                (list(exclude_item_ids), now_ms, publishers, artifacts, urls, READ_TARGETS_MAX),
            ).fetchall()
        ]

    def read_target_item(self, item_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT item_id, source_id, source_item_key, source_artifact_id, title, description,
                   canonical_url, reporting_origin, published_at_ms, observed_at_ms, first_ingest_mode,
                   evidence_text, evidence_text_sha256
              FROM news_items WHERE item_id = %s
            """,
            (item_id,),
        ).fetchone()
        return None if row is None else dict(row)
