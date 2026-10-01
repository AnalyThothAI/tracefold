"""Frozen source material and bounded semantic prior retrieval on the caller connection.

Commands use the caller's existing transaction; no external I/O or independent commit.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Final

from ..entities import source_mentions_asset
from ..events.gate import grounded_assets
from ..evidence import query_for
from ..models import MarketAsset, market_type_of
from ..similarity import trigram_similarity
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
)
from ..updates.identity import identity
from ..updates.projection import ReadingView, extraction_scopes, item_text, reading_view, reading_views
from .errors import EventUpdateConflict
from .evidence import EvidenceStorage

RELATED_PRIOR_EVENTS_MAX: Final = 8


RELATED_PRIOR_CLAIMS_MAX: Final = 8


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
            first_available_at_ms=int(item["observed_at_ms"]),
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


def _source_asset_candidates(
    material: Mapping[str, Any], evidence: Sequence[Evidence]
) -> dict[str, tuple[SourceAssetCandidate, ...]]:
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
                SourceAssetCandidate(
                    symbol=coin["symbol"],
                    market_type=market_type_of(coin.get("market_type")),
                    grade=None if coin.get("grade") is None else str(coin["grade"]),
                )
            )
        if rows:
            candidates[item.ref] = tuple(rows)
    return candidates


def _related_prior(
    documents: Sequence[Mapping[str, Any]],
    own: set[str],
    *,
    evidence: Sequence[Evidence],
    preferred_refs: set[str],
    task_texts: Sequence[str] | None = None,
) -> tuple[PriorClaim, ...]:
    """Rank the already recalled current claims before applying the unchanged eight-claim budget."""

    candidates: list[tuple[tuple[Any, ...], PriorClaim]] = []
    seen = set(own)
    for event_rank, document in enumerate(documents):
        try:
            head = EventUpdate.model_validate(document)
        except ValueError:
            # Another Event's unreadable head is that Event's fault; it is no comparison candidate here.
            log.warning("news_related_head_undecodable", extra={"event_id": document.get("event_id")})
            continue
        for claim in head.current_claims:
            if claim.ref in seen:
                continue
            seen.add(claim.ref)
            score = max(
                (
                    trigram_similarity(item_text, text)
                    for item_text in (task_texts if task_texts is not None else (row.text for row in evidence))
                    for text in (claim.statement, *(c.quote for c in claim.citations))
                ),
                default=0.0,
            )
            key = (claim.ref not in preferred_refs, -score, event_rank, claim.ref)
            candidates.append(
                (key, PriorClaim(event_id=head.event_id, content_revision=head.content_revision, claim=claim))
            )
    return tuple(row for _, row in sorted(candidates, key=lambda pair: pair[0])[:RELATED_PRIOR_CLAIMS_MAX])


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
    read consistently before comparing read refs; a copy of material already read or pending is not
    read again (`_unread`). Prior claims are this Event's adopted head claims plus
    a bounded set of related Events' head claims recalled by existing candidate retrieval. Assembly carries unaffected
    head claims, citations and relationships forward. Read targets are related Events' stored leader
    Items. Cashtags can retrieve candidates but do not resolve the claim's actor identity.
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
    all_candidates = _source_asset_candidates(material, complete)
    all_scopes = () if attached else extraction_scopes(material, complete)
    # A source ref proves only which body was stored, not which task boundary
    # was read.  Construct the current view before comparing completed reads.
    # A read that failed is settled too: it is quarantined until an exact reanalysis names it.
    completed = set((work or {}).get("processed_read_refs") or ()) | set((work or {}).get("failed_read_refs") or ())
    requested_read = (work or {}).get("reanalysis_read_ref")
    views = tuple(reading_view(event_id, row, all_scopes, all_candidates.get(row.ref, ())) for row in complete)
    if requested_read is None:
        unique = _unread(complete, views, completed)
    else:
        # An exact reanalysis reads the one named view, even when identical material was read before.
        unique = tuple(row for row, view in zip(complete, views, strict=True) if view.read_ref == requested_read)
        if not unique:
            raise EventUpdateConflict("news_reanalysis_read_scope_changed")
    selected = {row.ref for row in unique}
    scopes = tuple(scope for scope in all_scopes if scope.evidence_ref in selected)
    selected_views = tuple(view for view in views if view.evidence_ref in selected)
    prior: tuple[PriorClaim, ...] = ()
    if head_document is not None:
        head = EventUpdate.model_validate(head_document)
        prior = tuple(
            PriorClaim(event_id=head.event_id, content_revision=head.content_revision, claim=claim)
            for claim in head.current_claims
        )
    read_targets: tuple[ReadTarget, ...] = ()
    if not attached:
        # An optional read's revision re-asks only its focus claims against the attached material.
        preferred = {ref for row in prior for ref in row.claim.antecedent_refs}
        prior = (
            *prior,
            *_related_prior(
                material.get("related_heads") or (),
                {row.claim.ref for row in prior},
                evidence=unique,
                preferred_refs=preferred,
                task_texts=tuple(" ".join(span.text for span in view.spans) for view in selected_views),
            ),
        )
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
        work = self.conn.execute(
            """
            SELECT wanted_revision, done_revision, lineage_id, attached_evidence, focus_claim_refs,
                   processed_read_refs, failed_read_refs, last_outcome, last_error_code,
                   reanalysis_read_ref, reanalysis_reason, reanalysis_head_ref
              FROM news_semantic_work WHERE event_id = %s
            """,
            (event_id,),
        ).fetchone()
        snapshot = self.conn.execute(
            """
            SELECT s.evidence_version, s.snapshot,
                   (SELECT jsonb_object_agg(f.focus_fact_id, f.fact) FROM (
                      SELECT DISTINCT ON (h.focus_fact_id) h.focus_fact_id, h.snapshot -> 'focus_fact' AS fact
                        FROM news_event_evidence_snapshots h
                       WHERE h.event_id = s.event_id AND h.provenance = 'observed'
                         AND h.evidence_version <= s.evidence_version
                       ORDER BY h.focus_fact_id, h.evidence_version
                    ) f) AS fact_scopes
              FROM news_event_evidence_snapshots s
             WHERE s.event_id = %s AND s.provenance = 'observed'
             ORDER BY s.evidence_version DESC LIMIT 1
            """,
            (event_id,),
        ).fetchone()
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
                       canonical_url, reporting_origin, published_at_ms, observed_at_ms,
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
                SELECT r.item_id, r.revision_sha256, r.revision_sequence, r.evidence_text, r.reporting_origin,
                       r.source_artifact_id,
                       r.canonical_url, r.published_at_ms, r.received_at_ms
                  FROM news_item_revisions r
                  JOIN unnest(%s::text[], %s::text[]) AS frozen(item_id, revision_sha256)
                    ON frozen.item_id = r.item_id AND frozen.revision_sha256 = r.revision_sha256
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
        # Freeze pending reads before prior retrieval. An already processed leader or an unrelated
        # numbered sibling cannot supply the new task's text/source features.
        source = frozen_input(event_id, material)
        views = reading_views(source)
        task_texts = tuple(" ".join(span.text for span in view.spans) for view in views)
        source_items = tuple(
            {"source_artifact_id": row.source.artifact_id, "canonical_url": row.source.url} for row in source.evidence
        )
        # Provider tags belong to their immutable source, not the Event's old leader. A numbered
        # reading scope also needs visible evidence for the tag; source-wide grades cannot assign a
        # sibling's asset to this task. Whole-item reads retain the established provider grounding.
        metadata_by_item = _provider_metadata({**material, "items": ()})
        evidence_by_ref = {row.ref: row for row in source.evidence}
        assets: list[MarketAsset] = []
        for view, text in zip(views, task_texts, strict=True):
            source_item = evidence_by_ref[view.evidence_ref].source.record_id or ""
            coins = tuple(
                coin
                for coin in (metadata_by_item.get(source_item) or {}).get("coins") or ()
                if isinstance(coin, Mapping)
            )
            types = {str(coin.get("symbol")): market_type_of(coin.get("market_type")) for coin in coins}
            assets.extend(
                MarketAsset(symbol, types.get(symbol, "unknown"))
                for symbol in grounded_assets(text, coins)
                if view.mode != "scoped" or source_mentions_asset(symbol, types.get(symbol, "unknown"), text)
            )
        related_ids = (
            self._related_event_ids(
                event_id, now_ms=now_ms, task_texts=task_texts, source_items=source_items, assets=assets
            )
            if task_texts and not (work or {}).get("attached_evidence")
            else []
        )
        material["related_heads"] = self._related_head_documents(related_ids)
        material["read_targets"] = self._read_target_rows(related_ids, exclude_item_ids=item_ids)
        return material

    def _established_relations(self, event_id: str) -> list[dict[str, str]]:
        """Corrections and conflicts this Event's adopted revisions already published, by claim pair."""

        rows = self.conn.execute(
            """
            SELECT DISTINCT change->>'current_ref' AS current_ref, change->>'previous_ref' AS previous_ref,
                   change->>'relation' AS relation
              FROM news_event_updates u CROSS JOIN LATERAL jsonb_array_elements(u.document->'changes') change
             WHERE u.event_id = %s AND change->>'relation' IN ('corrects', 'conflicts')
             ORDER BY 1, 2, 3
            """,
            (event_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def _related_event_ids(
        self,
        event_id: str,
        *,
        now_ms: int,
        task_texts: Sequence[str],
        source_items: Sequence[Mapping[str, Any]],
        assets: Sequence[MarketAsset],
    ) -> list[str]:
        """Related Events by the existing bounded candidate retrieval, in its priority order."""

        query = query_for(
            event_id=event_id,
            cutoff=int(now_ms),
            assets=assets,
            task_texts=task_texts,
            source_items=source_items,
        )
        rows = self.evidence.evidence_candidates(query)
        ordered = sorted(
            rows, key=lambda row: (int(row["priority"]), -float(row["score"] or 0.0), str(row["event_id"]))
        )
        return [value for value in dict.fromkeys(str(row["event_id"]) for row in ordered) if value != event_id]

    def _related_head_documents(self, event_ids: Sequence[str]) -> list[dict[str, Any]]:
        if not event_ids:
            return []
        rows = self.conn.execute(
            """
            SELECT h.event_id, u.document
              FROM news_event_update_heads h
              JOIN news_event_updates u ON u.event_id = h.event_id AND u.content_revision = h.content_revision
             WHERE h.event_id = ANY(%s)
            """,
            (list(event_ids),),
        ).fetchall()
        by_event = {str(row["event_id"]): dict(row["document"]) for row in rows}
        return [by_event[value] for value in event_ids if value in by_event][:RELATED_PRIOR_EVENTS_MAX]

    def _read_target_rows(self, event_ids: Sequence[str], *, exclude_item_ids: Sequence[str]) -> list[dict[str, Any]]:
        if not event_ids:
            return []
        rows = self.conn.execute(
            """
            SELECT e.event_id, e.leader_item_id AS item_id, e.leader_title AS title
              FROM news_events e WHERE e.event_id = ANY(%s)
            """,
            (list(event_ids),),
        ).fetchall()
        by_event = {str(row["event_id"]): dict(row) for row in rows}
        excluded = set(exclude_item_ids)
        targets: list[dict[str, Any]] = []
        for value in event_ids:
            row = by_event.get(value)
            if row is None or str(row["item_id"]) in excluded:
                continue
            excluded.add(str(row["item_id"]))
            targets.append(row)
            if len(targets) >= READ_TARGETS_MAX:
                break
        return targets

    def read_target_item(self, item_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT item_id, source_id, source_item_key, source_artifact_id, title, description,
                   canonical_url, reporting_origin, published_at_ms, observed_at_ms,
                   evidence_text, evidence_text_sha256
              FROM news_items WHERE item_id = %s
            """,
            (item_id,),
        ).fetchone()
        return None if row is None else dict(row)
