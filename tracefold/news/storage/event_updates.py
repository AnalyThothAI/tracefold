"""EventUpdate adoption, semantic work, notification intents and the judgment cache (#706).

Every public method is one statement group run inside the caller's single short transaction
(`NewsDatabasePort.tx`/`read`). No model, provider, broker or send happens here. The async
`NewsStore` adapter in `event_update_store.py` owns which of these run together.

Identity rules owned here:
- `news_event_updates` and `news_semantic_{checkpoints,observations}` are insert-only; a replay returns
  the stored winner and a differing payload is an `EventUpdateConflict`.
- the adopted head is a CAS by `update_ref` under a per-Event advisory lock; its input revision never
  decreases.
- an `update` delivery intent is one queue row plus, from the moment it is frozen `sending`, one
  ledger row; the ledger row keeps the exact body, its digest and the provider message id.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Final, Literal, cast

from ..evidence import query_for
from ..models import MarketAsset, market_type_of
from ..reader_history import SIMILAR_TITLE_MAX, TARGETED_HISTORY_WINDOW_MS
from ..similarity import trigram_similarity
from ..taxonomy import source_authority
from ..updates.contracts import (
    EstablishedRelation,
    EventUpdate,
    Evidence,
    FrozenInput,
    IdentityHint,
    PriorClaim,
    ReadTarget,
    SemanticLease,
    Source,
)
from ..updates.identity import digest, identity
from ..updates.judgment import error_code
from ..updates.notification import (
    NEWS_CHANNEL,
    NOTIFICATION_ATTEMPTS_MAX,
    DeliveredText,
    FrozenCard,
    NotificationPlan,
)
from ..updates.ports import BeginSendStatus
from ..updates.projection import extraction_scopes, item_text, reading_view, reading_views
from .decisions import DecisionStorage
from .evidence import EvidenceStorage
from .sql_values import _dumps
from .update_commit import SemanticSource, commit_update, lock_event

log = logging.getLogger("tracefold.news")

SEMANTIC_ATTEMPTS_MAX: Final = 3
# Delay before the next attempt after the Nth failed one (1-based), per wanted revision.
SEMANTIC_RETRY_MS: Final = (15_000, 60_000, 300_000)
# A pending revision whose broker wake is older than this is woken again by the repair turn.
SEMANTIC_WAKE_STALE_MS: Final = 15_000
# Backoff after the first and second failed plan of one work; the third failure fails it.
NOTIFICATION_RETRY_MS: Final = (30_000, 120_000)
# How often work waiting on a send of its own Event still in flight looks again. Waiting spends no attempt.
NOTIFICATION_WAIT_MS: Final = 30_000
# A `sending` row older than this has no live owner: the provider call (8 s) and its bounded settlement
# (three short retries) are both long over, so the reconciliation may hold it ambiguous.
SENDING_ORPHAN_MS: Final = 60_000
ORPHAN_SEND_BATCH_MAX: Final = 50
# The queue's own CHECK bounds an intent at three attempts.
INTENT_ATTEMPTS_MAX: Final = 3
INTENT_RETRY_MS: Final = (30_000, 120_000, 600_000)
# Longer than one notification stage (20 s): compose, freeze and send happen under one lease.
# Covers one notification stage plus card composition and the send.
INTENT_LEASE_MS: Final = 120_000
RECEIPT_RECALL_MAX: Final = 16
JUDGMENT_CACHE_RETENTION_MS: Final = 14 * 24 * 3_600_000
PURGE_BATCH_MAX: Final = 1_000
READER_REVISION_PREFIX: Final = "reader_v2"
EXTRA_READ_OUTCOMES: Final = frozenset({"attached", "no_material", "unavailable_or_budget_exhausted"})
_RECEIPT_COLUMNS: Final = (
    "d.intent_id, d.event_id, d.kind, d.body, d.payload_sha256, d.settled_at_ms, "
    "d.receipt, d.card, d.history_context, d.claim_refs"
)
# Related Events whose adopted claims are compared with this Event's claims. Every current/prior pair
# is a relation question, so the recall is bounded by claims, not only by Events.
RELATED_PRIOR_EVENTS_MAX: Final = 8
RELATED_PRIOR_CLAIMS_MAX: Final = 8
# Code-prepared optional read targets: related Events' leader Items not already in the input.
READ_TARGETS_MAX: Final = 4
READ_TARGET_PREFIX: Final = "news_item:"
_WAKE_STATE_LIMIT: Final = 1_000
# A wanted revision that can still be claimed: attempts remain and it has not failed. A failed revision keeps
# its real attempt count; only new evidence, a reanalysis or an explicit retry makes it runnable again.
_RUNNABLE: Final = f"attempts < {SEMANTIC_ATTEMPTS_MAX} AND last_outcome IS DISTINCT FROM 'failed'"
SEMANTIC_WAKE_STATE_SQL: Final = f"""
    WITH pending AS MATERIALIZED (
      SELECT attempts, last_outcome, updated_at_ms FROM news_semantic_work
       WHERE done_revision IS NULL OR done_revision < wanted_revision
       ORDER BY next_attempt_at_ms, event_id
       LIMIT {_WAKE_STATE_LIMIT}
    )
    SELECT count(*) FILTER (WHERE {_RUNNABLE}) AS pending,
           min(updated_at_ms) FILTER (WHERE {_RUNNABLE}) AS oldest_pending_at_ms,
           count(*) FILTER (WHERE last_outcome = 'failed') AS expired
      FROM pending
"""  # noqa: S608 - code-owned integer constants only
# The semantic stage's 24 h health: completed turns, adoptions and visibly failed work. Model health reads
# these, not legacy verdicts; pending work is bounded like the wake state.
SEMANTIC_STATUS_SQL: Final = f"""
    WITH outstanding AS MATERIALIZED (
      SELECT attempts, last_outcome, next_attempt_at_ms, leased_until_ms
        FROM news_semantic_work
       WHERE done_revision IS NULL OR done_revision < wanted_revision
       ORDER BY next_attempt_at_ms, event_id
       LIMIT {_WAKE_STATE_LIMIT}
    )
    SELECT
      (SELECT count(*) FROM news_semantic_observations WHERE completed_at_ms >= %(since)s)
        AS semantic_observations_24h,
      (SELECT count(*) FROM news_event_updates WHERE adopted_at_ms >= %(since)s) AS semantic_adopted_24h,
      (SELECT count(*) FROM news_semantic_work WHERE last_outcome = 'failed' AND updated_at_ms >= %(since)s)
        AS semantic_failed_24h,
      (SELECT count(*) FROM outstanding WHERE {_RUNNABLE}
         AND next_attempt_at_ms <= %(now)s AND (leased_until_ms IS NULL OR leased_until_ms <= %(now)s))
        AS semantic_pending,
      (SELECT count(*) FROM outstanding WHERE {_RUNNABLE}
         AND next_attempt_at_ms > %(now)s) AS semantic_deferred,
      (SELECT count(*) FROM outstanding WHERE leased_until_ms > %(now)s) AS semantic_in_progress,
      (SELECT count(*) FROM outstanding WHERE last_outcome = 'failed') AS semantic_failed_exhausted
"""  # noqa: S608 - code-owned integer constant only
SEMANTIC_FAILED_CODES_SQL: Final = """
    SELECT COALESCE(last_error_code, 'unknown') AS code, count(*) AS n
      FROM news_semantic_work
     WHERE last_outcome = 'failed' AND updated_at_ms >= %s
     GROUP BY 1
"""

IntentOutcome = Literal["sent", "not_sent", "ambiguous"]


class EventUpdateConflict(ValueError):
    """A stored insert-only fact disagrees with the value offered for the same identity."""


class IntentLeaseLost(RuntimeError):
    """The caller no longer owns the intent lease it is writing under."""


class SemanticLeaseLost(RuntimeError):
    """The semantic attempt no longer owns its frozen input's work."""


def _retry_delay(delays: Sequence[int], attempts: int) -> int:
    return int(delays[max(0, min(int(attempts), len(delays)) - 1)])


def reader_revision(
    receipts: Iterable[Mapping[str, Any]],
    *,
    blocked_claim_refs: Iterable[str],
    ambiguous_claim_refs: Iterable[str],
    watch_symbols: Iterable[str],
    invalidated_claim_refs: Iterable[str],
) -> str:
    """One Event's reader version: its related receipts, its unsettled claims and the watchlist.

    It is the same digest whenever nothing a plan of this Event depends on has changed, so a plan
    decided twice from the same reader is the same decision, and a send anywhere else is not a race.
    """

    material = {
        "receipts": sorted({(str(row["intent_id"]), "sent") for row in receipts}),
        "blocked": sorted(set(blocked_claim_refs)),
        "ambiguous": sorted(set(ambiguous_claim_refs)),
        "invalidated": sorted(set(invalidated_claim_refs)),
        "watch": sorted(set(watch_symbols)),
    }
    return f"{READER_REVISION_PREFIX}:{digest(material)}"


def linked_refs(update: EventUpdate, invalidated: Iterable[str] = ()) -> set[str]:
    """The active claims of an update and their antecedents: what a linked receipt carried."""

    inactive = set(update.retired_claim_refs) | set(update.superseded_claim_refs) | set(invalidated)
    return {ref for claim in update.claims if claim.ref not in inactive for ref in (claim.ref, *claim.antecedent_refs)}


def delivered_text(row: Mapping[str, Any]) -> DeliveredText | None:
    """One sent receipt with a provably exact frozen body for coverage judgment."""

    body = row.get("body")
    payload_sha256 = row.get("payload_sha256")
    # Malformed or incomplete receipts cannot prove exact reader coverage.
    if not isinstance(body, str) or not body or not isinstance(payload_sha256, str):
        return None
    receipt = row.get("receipt") or {}
    message_id = None
    if isinstance(receipt, Mapping):
        value = receipt.get("provider_message_id", receipt.get("message_id"))
        message_id = None if value is None else str(value)
    return DeliveredText(
        intent_id=str(row["intent_id"]),
        channel=NEWS_CHANNEL,
        state="sent",
        body=body,
        payload_sha256=payload_sha256,
        received_at_ms=int(row["settled_at_ms"]),
        provider_message_id=message_id,
    )


def receipt_queries(
    update: EventUpdate,
    comparison_title: str,
    invalidated: Iterable[str] = (),
) -> tuple[str, ...]:
    """Source-language and adopted-claim views; later members need not match the leader title."""
    inactive = set(update.retired_claim_refs) | set(update.superseded_claim_refs) | set(invalidated)
    queries: list[str] = []
    for claim in update.claims:
        if claim.ref in inactive:
            continue
        queries.extend((claim.statement, " ".join((claim.fields.subject, claim.fields.action, claim.fields.object))))
        queries.extend(citation.quote for citation in claim.citations)
    return tuple(dict.fromkeys(text.strip() for text in (queries or [comparison_title]) if text.strip()))


def select_receipts(
    update: EventUpdate,
    queries: Sequence[str],
    rows: Sequence[Mapping[str, Any]],
    *,
    invalidated: Iterable[str] = (),
    limit: int = RECEIPT_RECALL_MAX,
) -> tuple[DeliveredText, ...]:
    """Rank actual receipts together before budgeting; an incremental card never replaces earlier copy.

    Explicit claim/antecedent references rank first, then content relevance and recency. Neither same
    Event nor asset-band membership is a coverage verdict. Only the planner reads the sent body for that.
    """
    refs = linked_refs(update, invalidated)
    ranked = []
    seen: set[str] = set()
    for row in rows:
        receipt = delivered_text(row)
        if receipt is None or receipt.intent_id in seen:
            continue
        seen.add(receipt.intent_id)
        context = row.get("history_context") or {}
        views = (receipt.body, str(context.get("comparison_title") or ""), str(context.get("headline_zh") or ""))
        score = max((trigram_similarity(query, view) for query in queries for view in views if view), default=0.0)
        linked = bool(refs.intersection(row.get("claim_refs") or ()))
        ranked.append((-int(linked), -score, -int(receipt.received_at_ms or 0), receipt.intent_id, receipt))
    ranked.sort(key=lambda row: row[:4])
    log.info(
        "news_receipt_recall",
        extra={
            "event_id": update.event_id,
            "candidate_count": len(ranked),
            "limit": limit,
            "ranked": [
                {"intent_id": row[3], "linked": bool(-row[0]), "score": -row[1], "selected": index < limit}
                for index, row in enumerate(ranked[: limit * 2])
            ],
        },
    )
    return tuple(row[4] for row in ranked[:limit])


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


def _identity_hints(
    evidence: Sequence[Evidence], symbols: Iterable[str], *, visible_text: Mapping[str, str] | None = None
) -> tuple[IdentityHint, ...]:
    """Code-owned identities only: a Gate-grounded asset written as its cashtag in the evidence.

    The hint refuses an `equivalent` answer between claims whose quotes name different grounded
    assets; it never asserts equality and is never inferred from a model or a name.
    """

    hints: list[IdentityHint] = []
    for symbol in sorted({str(value).strip().upper() for value in symbols if str(value).strip()}):
        surface = f"${symbol}"
        hints.extend(
            IdentityHint(key="subject_id", value=symbol, evidence_ref=item.ref, surface=surface)
            for item in evidence
            if surface in (visible_text[item.ref] if visible_text is not None else item.text)
        )
    return tuple(hints)


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


def frozen_input(event_id: str, material: Mapping[str, Any]) -> FrozenInput:
    """Assemble the frozen semantic input from one consistent read.

    Evidence contains task reads not yet recorded for this Event: newly joined members, changed task
    scopes, later bodies of an existing Item, or a bounded optional read. The complete snapshot is
    read consistently before comparing read refs. Prior claims are this Event's adopted head claims plus
    a bounded set of related Events' head claims recalled by existing candidate retrieval. Assembly carries unaffected
    head claims, citations and relationships forward. Read targets are related Events' stored leader
    Items; identity hints are Gate-grounded cashtags in the new material.
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
    all_scopes = () if attached else extraction_scopes(material, complete)
    # A source ref proves only which body was stored, not which task boundary
    # was read.  Construct the current view before comparing completed reads.
    # A read that failed is settled too: it is quarantined until an exact reanalysis names it.
    completed = set((work or {}).get("processed_read_refs") or ()) | set((work or {}).get("failed_read_refs") or ())
    requested_read = (work or {}).get("reanalysis_read_ref")
    views = tuple(reading_view(event_id, row, all_scopes) for row in complete)
    unique = tuple(
        row
        for row, view in zip(complete, views, strict=True)
        if (view.read_ref == requested_read if requested_read is not None else view.read_ref not in completed)
    )
    if requested_read is not None and not unique:
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
    hints: tuple[IdentityHint, ...] = ()
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
        hints = _identity_hints(
            unique,
            material.get("grounded_assets") or (),
            visible_text={view.evidence_ref: " ".join(span.text for span in view.spans) for view in selected_views},
        )
    facing = set(material.get("reader_facing_claim_refs") or ())
    prior = tuple(row.model_copy(update={"reader_facing": row.claim.ref in facing}) for row in prior)
    wanted = int(work["wanted_revision"]) if work is not None else max(1, int(material.get("evidence_version") or 1))
    lineage = str(work["lineage_id"]) if work is not None else identity("lineage", event_id, wanted)
    return FrozenInput(
        event_id=event_id,
        revision=wanted,
        lineage_id=lineage,
        evidence=unique,
        extraction_scopes=scopes,
        prior=prior,
        read_targets=read_targets,
        focus_claim_refs=focus,
        open_questions={}
        if head_document is None
        else {row.ref: row for row in EventUpdate.model_validate(head_document).open_questions},
        identity_hints=hints,
        established_relations=tuple(
            EstablishedRelation.model_validate(row) for row in material.get("established_relations") or ()
        ),
        reanalysis_reason=None if work is None else work.get("reanalysis_reason"),
        reanalysis_head_ref=None if work is None else work.get("reanalysis_head_ref"),
    )


class EventUpdateStorage:
    conn: Any

    # ------------------------------------------------------------------ semantic work (admission/U2)
    def request_semantic_revision(self, *, event_id: str, lineage_id: str, now_ms: int) -> int:
        """Want one more semantic revision of this Event, in the transaction that appended its evidence.

        The revision counter belongs to semantic work, so an optional read's revision and a new organic
        evidence snapshot can never collide. A new organic revision starts `lineage_id` afresh: its
        attempts, broker wake, one-read budget and attached material are reset.
        """

        row = self.conn.execute(
            """
            INSERT INTO news_semantic_work (
              event_id, wanted_revision, lineage_id, attempts, next_attempt_at_ms, updated_at_ms
            ) VALUES (%s, 1, %s, 0, %s, %s)
            ON CONFLICT (event_id) DO UPDATE SET
              wanted_revision = news_semantic_work.wanted_revision + 1,
              lineage_id = EXCLUDED.lineage_id,
              attempts = 0,
              next_attempt_at_ms = EXCLUDED.next_attempt_at_ms,
              published_at_ms = NULL,
              last_outcome = NULL,
              last_error_code = NULL,
              extra_read_state = NULL,
              extra_read_target_ref = NULL,
              attached_evidence = NULL,
              focus_claim_refs = NULL,
              reanalysis_read_ref = NULL,
              reanalysis_reason = NULL,
              reanalysis_head_ref = NULL,
              updated_at_ms = EXCLUDED.updated_at_ms
            RETURNING wanted_revision
            """,
            (event_id, lineage_id, int(now_ms), int(now_ms)),
        ).fetchone()
        return int(row["wanted_revision"])

    def mark_semantic_work_published(self, *, event_id: str, revision: int, now_ms: int) -> bool:
        """Record the broker wake of one wanted revision; a newer revision keeps its own unwoken marker."""

        cursor = self.conn.execute(
            """
            UPDATE news_semantic_work SET published_at_ms = %s
             WHERE event_id = %s AND wanted_revision = %s
            """,
            (int(now_ms), event_id, int(revision)),
        )
        return bool(cursor.rowcount)

    def claim_semantic_work(
        self, *, event_id: str, lease_token: str, now_ms: int, lease_ms: int
    ) -> SemanticLease | None:
        """Lease due pending work, spending one attempt of its wanted revision.

        The frozen input is read in the same transaction. When the stored material cannot form one (a missing
        body, a changed reanalysis scope, an undecodable head or source), only this revision fails, visibly and
        with its code; the consumer and every other Event keep running.
        """

        row = self.conn.execute(
            f"""
            UPDATE news_semantic_work
               SET attempts = attempts + 1, lease_token = %s, leased_until_ms = %s, updated_at_ms = %s
             WHERE event_id = %s
               AND (done_revision IS NULL OR done_revision < wanted_revision)
               AND {_RUNNABLE}
               AND next_attempt_at_ms <= %s
               AND (leased_until_ms IS NULL OR leased_until_ms <= %s)
            RETURNING event_id, wanted_revision, lineage_id, lease_token, attempts
            """,  # noqa: S608 - code-owned predicate only
            (lease_token, int(now_ms) + int(lease_ms), int(now_ms), event_id, int(now_ms), int(now_ms)),
        ).fetchone()
        if row is None:
            return None
        try:
            source = frozen_input(event_id, self.semantic_input_material(event_id, now_ms=now_ms))
        except (LookupError, ValueError) as exc:
            # EventUpdateConflict and pydantic's ValidationError are ValueErrors.
            code = error_code(exc, default="news_semantic_input_invalid")
            log.warning("news semantic input failed event_id=%s code=%s", event_id, code)
            self.conn.execute(
                "UPDATE news_semantic_work SET lease_token=NULL, leased_until_ms=NULL, last_outcome='failed',"
                " last_error_code=%s, updated_at_ms=%s WHERE event_id=%s",
                (code, int(now_ms), event_id),
            )
            return None
        return SemanticLease(source=source, lease_token=str(row["lease_token"]), attempts=int(row["attempts"]))

    def require_semantic_owner(self, lease: SemanticLease, *, now_ms: int) -> Mapping[str, Any]:
        row = self.conn.execute(
            "SELECT wanted_revision, attempts FROM news_semantic_work "
            "WHERE event_id=%s AND lease_token=%s AND leased_until_ms>%s FOR UPDATE",
            (lease.event_id, lease.lease_token, int(now_ms)),
        ).fetchone()
        if row is None:
            raise SemanticLeaseLost("news_semantic_lease_lost")
        return dict(row)

    def defer_semantic_event(self, *, lease: SemanticLease, reason: str, now_ms: int, retry_after_ms: int = 0) -> bool:
        """Settle only this input's retry budget; newer evidence remains due."""
        return self._end_semantic_attempt(
            lease, reason=reason, now_ms=now_ms, retry_after_ms=retry_after_ms, failed=False
        )

    def fail_semantic_event(self, *, lease: SemanticLease, error_code: str, now_ms: int) -> bool:
        return self._end_semantic_attempt(lease, reason=error_code, now_ms=now_ms, failed=True)

    def _end_semantic_attempt(
        self, lease: SemanticLease, *, reason: str, now_ms: int, failed: bool, retry_after_ms: int = 0
    ) -> bool:
        """Settle one attempt. A revision that ends failed keeps its real attempt count and quarantines the
        task reads it was given: later revisions read only newer material, and the failed reads stay listed
        for an exact reanalysis."""

        try:
            row = self.require_semantic_owner(lease, now_ms=now_ms)
        except SemanticLeaseLost:
            return False
        attempts = int(row["attempts"])
        failed = failed or attempts >= SEMANTIC_ATTEMPTS_MAX
        quarantined = [view.read_ref for view in reading_views(lease.source)] if failed else []
        newer = int(row["wanted_revision"]) > lease.wanted_revision
        if newer:
            self.conn.execute(
                "UPDATE news_semantic_work SET lease_token=NULL, leased_until_ms=NULL,"
                " failed_read_refs=ARRAY(SELECT DISTINCT ref FROM unnest(failed_read_refs || %s::text[]) AS ref)"
                " WHERE event_id=%s",
                (quarantined, lease.event_id),
            )
        else:
            self.conn.execute(
                "UPDATE news_semantic_work SET lease_token=NULL, leased_until_ms=NULL, last_outcome=%s,"
                " last_error_code=%s, next_attempt_at_ms=%s, updated_at_ms=%s,"
                " failed_read_refs=ARRAY(SELECT DISTINCT ref FROM unnest(failed_read_refs || %s::text[]) AS ref)"
                " WHERE event_id=%s",
                (
                    "failed" if failed else reason,
                    reason,
                    int(now_ms) + max(retry_after_ms, _retry_delay(SEMANTIC_RETRY_MS, attempts)),
                    int(now_ms),
                    quarantined,
                    lease.event_id,
                ),
            )
        return True

    def finish_semantic_work(self, *, work_id: str, lease: SemanticLease, reason: str, now_ms: int) -> bool:
        row = self.require_semantic_owner(lease, now_ms=now_ms)
        observed = self._observed_work(work_id)
        if observed["event_id"] != lease.event_id or observed["input_revision"] != lease.wanted_revision:
            raise EventUpdateConflict("news_semantic_observation_lease_mismatch")
        current = int(row["wanted_revision"]) == lease.wanted_revision
        cursor = self.conn.execute(
            """
            UPDATE news_semantic_work
               SET done_revision = GREATEST(COALESCE(done_revision, 0), %s),
                   processed_read_refs = ARRAY(
                       SELECT DISTINCT ref FROM unnest(processed_read_refs || %s::text[]) AS ref
                   ),
                   failed_read_refs = ARRAY(
                       SELECT ref FROM unnest(failed_read_refs) AS ref WHERE ref <> ALL(%s::text[])
                   ),
                   reanalysis_read_ref = CASE WHEN %s THEN NULL ELSE reanalysis_read_ref END,
                   reanalysis_reason = CASE WHEN %s THEN NULL ELSE reanalysis_reason END,
                   reanalysis_head_ref = CASE WHEN %s THEN NULL ELSE reanalysis_head_ref END,
                   attempts = CASE WHEN %s THEN 0 ELSE attempts END,
                   lease_token = NULL, leased_until_ms = NULL,
                   last_outcome = CASE WHEN %s THEN %s ELSE last_outcome END,
                   last_error_code = CASE WHEN %s THEN NULL ELSE last_error_code END,
                   next_attempt_at_ms = CASE WHEN %s THEN %s ELSE next_attempt_at_ms END,
                   updated_at_ms = %s
             WHERE event_id = %s
            """,
            (
                observed["input_revision"],
                list(observed["read_refs"]),
                list(observed["read_refs"]),
                current,
                current,
                current,
                current,
                current,
                reason,
                current,
                current,
                int(now_ms),
                int(now_ms),
                lease.event_id,
            ),
        )
        return bool(cursor.rowcount)

    def _observed_work(self, work_id: str) -> Mapping[str, Any]:
        # The port addresses work by its code-owned identity; its Event and input revision are the ones
        # the observation of that work recorded. Every service path saves one before finishing.
        rows = self.conn.execute(
            """
            SELECT event_id, input_revision, read_refs
              FROM news_semantic_observations WHERE work_id = %s
            """,
            (work_id,),
        ).fetchall()
        if not rows or len({str(row["event_id"]) for row in rows}) != 1:
            raise LookupError("news_semantic_work_unknown")
        return {
            "event_id": str(rows[0]["event_id"]),
            "input_revision": max(int(row["input_revision"]) for row in rows),
            "read_refs": tuple({ref for row in rows for ref in row["read_refs"]}),
        }

    def terminalize_exhausted_semantic_work(self, *, now_ms: int, limit: int) -> int:
        """The Janitor settles a crashed final attempt only after its lease expires."""

        cursor = self.conn.execute(
            """
            WITH expired AS (
              SELECT event_id FROM news_semantic_work
               WHERE (done_revision IS NULL OR done_revision < wanted_revision)
                 AND attempts >= %s AND last_outcome IS DISTINCT FROM 'failed'
                 AND (leased_until_ms IS NULL OR leased_until_ms <= %s)
               ORDER BY updated_at_ms, event_id LIMIT %s FOR UPDATE SKIP LOCKED
            )
            UPDATE news_semantic_work w
               SET last_outcome = 'failed', last_error_code = 'news_semantic_attempts_exhausted_after_lease',
                   lease_token = NULL, leased_until_ms = NULL, updated_at_ms = %s
              FROM expired WHERE w.event_id = expired.event_id
            """,
            (SEMANTIC_ATTEMPTS_MAX, int(now_ms), int(limit), int(now_ms)),
        )
        return int(cursor.rowcount)

    def pending_semantic_event_ids(self, *, now_ms: int, limit: int) -> list[str]:
        rows = self.conn.execute(
            f"""
            SELECT event_id FROM news_semantic_work
             WHERE (done_revision IS NULL OR done_revision < wanted_revision)
               AND {_RUNNABLE}
               AND next_attempt_at_ms <= %s
               AND (leased_until_ms IS NULL OR leased_until_ms <= %s)
               AND (published_at_ms IS NULL OR published_at_ms <= %s)
             ORDER BY next_attempt_at_ms, event_id
             LIMIT %s
            """,  # noqa: S608 - code-owned predicate only
            (int(now_ms), int(now_ms), int(now_ms) - SEMANTIC_WAKE_STALE_MS, int(limit)),
        ).fetchall()
        return [str(row["event_id"]) for row in rows]

    def semantic_work(self, event_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM news_semantic_work WHERE event_id = %s", (event_id,)).fetchone()
        return None if row is None else dict(row)

    def reanalysis_scope_list(self, *, event_id: str, now_ms: int) -> dict[str, Any]:
        """Inspect the exact current task reads without changing semantic work."""

        material = self.semantic_input_material(event_id, now_ms=now_ms)
        work = material.get("work")
        if work is None:
            raise LookupError("news_reanalysis_event_work_missing")
        if work.get("attached_evidence"):
            raise EventUpdateConflict("news_reanalysis_optional_read_pending")
        complete_work = {**work, "processed_read_refs": (), "failed_read_refs": (), "reanalysis_read_ref": None}
        source = frozen_input(event_id, {**material, "work": complete_work})
        completed = set(work.get("processed_read_refs") or ())
        failed = set(work.get("failed_read_refs") or ())
        head = material.get("head")
        return {
            "event_id": event_id,
            "wanted_revision": int(work["wanted_revision"]),
            "done_revision": work.get("done_revision"),
            "failed": work.get("last_outcome") == "failed",
            "last_error_code": work.get("last_error_code"),
            "head_revision": None if head is None else str(head["content_revision"]),
            "scopes": [
                {
                    "evidence_ref": view.evidence_ref,
                    "source_version": view.source_version,
                    "fact_ids": [
                        scope.fact_id for scope in source.extraction_scopes if scope.evidence_ref == view.evidence_ref
                    ],
                    "read_ref": view.read_ref,
                    "mode": view.mode,
                    "reason": view.reason,
                    "material_sha": view.material_sha,
                    "visible_chars": sum(len(span.text) for span in view.spans),
                    "completed": view.read_ref in completed,
                    "failed": view.read_ref in failed,
                }
                for view in (reading_view(event_id, item, source.extraction_scopes) for item in source.evidence)
            ],
        }

    def request_reanalysis(
        self,
        *,
        event_id: str,
        expected_wanted_revision: int,
        expected_head_revision: str | None,
        read_ref: str,
        reason: str,
        now_ms: int,
    ) -> int:
        """Open one system revision for one inspected task view, under version CAS.

        The inspected revision must be settled: done, or failed. Reanalysing a failed revision reads exactly
        the named (possibly quarantined) task view again; the rest of the quarantine stays in place.
        """

        if not reason.strip() or not read_ref:
            raise ValueError("news_reanalysis_target_or_reason_missing")
        row = self.conn.execute(
            "SELECT wanted_revision, done_revision, leased_until_ms, last_outcome FROM news_semantic_work "
            "WHERE event_id=%s FOR UPDATE",
            (event_id,),
        ).fetchone()
        if row is None:
            raise LookupError("news_reanalysis_event_work_missing")
        settled = row["done_revision"] == expected_wanted_revision or row["last_outcome"] == "failed"
        if int(row["wanted_revision"]) != expected_wanted_revision or not settled:
            raise EventUpdateConflict("news_reanalysis_wanted_revision_changed_or_incomplete")
        if row["leased_until_ms"] is not None and int(row["leased_until_ms"]) > now_ms:
            raise EventUpdateConflict("news_reanalysis_lease_active")
        current_head = self.conn.execute(
            "SELECT content_revision FROM news_event_update_heads WHERE event_id=%s", (event_id,)
        ).fetchone()
        head_revision = None if current_head is None else str(current_head["content_revision"])
        if head_revision != expected_head_revision:
            raise EventUpdateConflict("news_reanalysis_head_changed")
        listing = self.reanalysis_scope_list(event_id=event_id, now_ms=now_ms)
        if read_ref not in {entry["read_ref"] for entry in listing["scopes"]}:
            raise EventUpdateConflict("news_reanalysis_read_scope_changed")
        next_revision = expected_wanted_revision + 1
        self.conn.execute(
            """
            UPDATE news_semantic_work
               SET wanted_revision=%s, lineage_id=%s, attempts=0, next_attempt_at_ms=%s,
                   published_at_ms=NULL, last_outcome=NULL, last_error_code=NULL,
                   lease_token=NULL, leased_until_ms=NULL,
                   reanalysis_read_ref=%s, reanalysis_reason=%s, reanalysis_head_ref=%s,
                   updated_at_ms=%s
             WHERE event_id=%s
            """,
            (
                next_revision,
                identity("lineage_reanalysis", event_id, next_revision, read_ref),
                int(now_ms),
                read_ref,
                reason.strip(),
                None if head_revision is None else identity("update", event_id, head_revision),
                int(now_ms),
                event_id,
            ),
        )
        return next_revision

    # ------------------------------------------------------------------ semantic input and results
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
        items = (
            self.conn.execute(
                """
                SELECT item_id, source_id, source_item_key, source_artifact_id, title, description,
                       canonical_url, reporting_origin, published_at_ms, observed_at_ms,
                       evidence_text, evidence_text_sha256
                  FROM news_items WHERE item_id = ANY(%s)
                """,
                (item_ids,),
            ).fetchall()
            if item_ids
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
        leader = next((dict(row) for row in items if str(row["item_id"]) == str(card.get("leader_item_id"))), None)
        related_ids = [] if leader is None else self._related_event_ids(event_id, card, leader, now_ms=now_ms)
        return {
            "work": None if work is None else dict(work),
            "evidence_version": None if snapshot is None else int(snapshot["evidence_version"]),
            "item_ids": item_ids,
            "members": members,
            "fact_scopes": {} if snapshot is None else dict(snapshot["fact_scopes"] or {}),
            "items": [dict(row) for row in items],
            "revisions": [dict(row) for row in revisions],
            "head": self.event_update_head_document(event_id),
            "established_relations": self._established_relations(event_id),
            "reader_facing_claim_refs": self._reader_facing_claim_refs([event_id, *related_ids]),
            "related_heads": self._related_head_documents(related_ids),
            "read_targets": self._read_target_rows(related_ids, exclude_item_ids=item_ids),
            "grounded_assets": [str(value) for value in card.get("grounded_assets") or ()],
        }

    def _reader_facing_claim_refs(self, event_ids: Sequence[str]) -> list[str]:
        """Claims of these Events a reader card carried, carries or may still carry.

        Queued, sending or settled cards name their claims; an Event whose notification is still undecided
        may yet carry any claim of its current head.
        """

        rows = self.conn.execute(
            """
            SELECT DISTINCT ref FROM (
              SELECT jsonb_array_elements_text(claim_refs) AS ref FROM news_deliveries
               WHERE event_id = ANY(%s) AND kind = 'update' AND state IN ('sending', 'sent', 'ambiguous')
              UNION ALL
              SELECT jsonb_array_elements_text(claim_refs) FROM news_delivery_queue
               WHERE event_id = ANY(%s) AND kind = 'update' AND state = 'pending' AND claim_refs IS NOT NULL
              UNION ALL
              SELECT jsonb_array_elements(u.document -> 'claims') ->> 'ref'
                FROM news_notification_work w
                JOIN news_event_update_heads h ON h.event_id = w.event_id
                JOIN news_event_updates u ON u.event_id = h.event_id AND u.content_revision = h.content_revision
               WHERE w.event_id = ANY(%s) AND w.channel = %s AND w.state = 'pending'
            ) facing ORDER BY ref
            """,
            (list(event_ids), list(event_ids), list(event_ids), NEWS_CHANNEL),
        ).fetchall()
        return [str(row["ref"]) for row in rows]

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
        self, event_id: str, card: Mapping[str, Any], leader: Mapping[str, Any], *, now_ms: int
    ) -> list[str]:
        """Related Events by the existing bounded candidate retrieval, in its priority order."""

        coins = (card.get("provider_metadata") or {}).get("coins") or ()
        types = {
            str(coin.get("symbol")): market_type_of(coin.get("market_type"))
            for coin in coins
            if isinstance(coin, Mapping)
        }
        assets = tuple(
            MarketAsset(str(symbol), types.get(str(symbol), "unknown")) for symbol in card.get("grounded_assets") or ()
        )
        query = query_for({**card, "event_id": event_id}, leader, cutoff=int(now_ms), assets=assets)
        rows = cast(EvidenceStorage, self).evidence_candidates(query)
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
              FROM news_items WHERE item_id = %s AND market_kind IS NULL
            """,
            (item_id,),
        ).fetchone()
        return None if row is None else dict(row)

    def semantic_wake_route(self, event_id: str) -> dict[str, Any] | None:
        """What a broker wake of pending semantic work carries: its revision and its routing key parts."""

        row = self.conn.execute(
            """
            SELECT w.event_id, w.wanted_revision, e.dedupe_family, e.queue_priority, e.trace_id
              FROM news_semantic_work w JOIN news_events e ON e.event_id = w.event_id
             WHERE w.event_id = %s AND (w.done_revision IS NULL OR w.done_revision < w.wanted_revision)
            """,
            (event_id,),
        ).fetchone()
        return None if row is None else dict(row)

    def semantic_status(self, *, now_ms: int) -> dict[str, Any]:
        """The semantic stage's last 24 h, for the status page's model health."""

        since = int(now_ms) - 24 * 3_600_000
        row = self.conn.execute(SEMANTIC_STATUS_SQL, {"since": since, "now": int(now_ms)}).fetchone()
        codes = self.conn.execute(SEMANTIC_FAILED_CODES_SQL, (since,)).fetchall()
        values = {key: int(value or 0) for key, value in dict(row or {}).items()}
        return {
            "semantic_observations_24h": values.get("semantic_observations_24h", 0),
            "semantic_adopted_24h": values.get("semantic_adopted_24h", 0),
            "semantic_failed_24h": values.get("semantic_failed_24h", 0),
            "semantic_pending": values.get("semantic_pending", 0),
            "semantic_deferred": values.get("semantic_deferred", 0),
            "semantic_in_progress": values.get("semantic_in_progress", 0),
            "semantic_failed_exhausted": values.get("semantic_failed_exhausted", 0),
            "semantic_failed_by_code_24h": {str(r["code"]): int(r["n"]) for r in codes},
        }

    def semantic_wake_state(self) -> dict[str, int | None]:
        """Bounded pending/exhausted semantic work for maintenance telemetry."""

        row = self.conn.execute(SEMANTIC_WAKE_STATE_SQL).fetchone()
        return {
            "pending": int(row["pending"] or 0) if row else 0,
            "oldest_pending_at_ms": None
            if row is None or row["oldest_pending_at_ms"] is None
            else int(row["oldest_pending_at_ms"]),
            "expired": int(row["expired"] or 0) if row else 0,
        }

    def event_update_head_document(self, event_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT u.document
              FROM news_event_update_heads h
              JOIN news_event_updates u ON u.event_id = h.event_id AND u.content_revision = h.content_revision
             WHERE h.event_id = %s
            """,
            (event_id,),
        ).fetchone()
        return None if row is None else dict(row["document"])

    def semantic_checkpoint_documents(self, work_id: str) -> dict[str, dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT stage, document FROM news_semantic_checkpoints WHERE work_id = %s", (work_id,)
        ).fetchall()
        return {str(row["stage"]): dict(row["document"]) for row in rows}

    def insert_semantic_checkpoint(
        self, *, work_id: str, stage: str, document_json: str, now_ms: int
    ) -> dict[str, Any]:
        """Insert-only; the first stored stage document wins a race and is returned."""

        self.conn.execute(
            """
            INSERT INTO news_semantic_checkpoints (work_id, stage, document, created_at_ms)
            VALUES (%s, %s, %s::jsonb, %s)
            ON CONFLICT (work_id, stage) DO NOTHING
            """,
            (work_id, stage, document_json, int(now_ms)),
        )
        row = self.conn.execute(
            "SELECT document FROM news_semantic_checkpoints WHERE work_id = %s AND stage = %s", (work_id, stage)
        ).fetchone()
        return dict(row["document"])

    def insert_semantic_observation(
        self,
        *,
        result_id: str,
        work_id: str,
        event_id: str,
        input_revision: int,
        input_sha256: str,
        program_identity: str,
        completed_at_ms: int,
        understanding_json: str,
        read_refs: Sequence[str],
        reanalysis_reason: str | None,
        reanalysis_head_ref: str | None,
    ) -> dict[str, Any]:
        """Insert-only by result id; the stored row, with its original completion clock, is returned."""

        self.conn.execute(
            """
            INSERT INTO news_semantic_observations (
              result_id, work_id, event_id, input_revision, input_sha256, program_identity,
              completed_at_ms, understanding, read_refs, reanalysis_reason, reanalysis_head_ref
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s)
            ON CONFLICT (result_id) DO NOTHING
            """,
            (
                result_id,
                work_id,
                event_id,
                int(input_revision),
                input_sha256,
                program_identity,
                int(completed_at_ms),
                understanding_json,
                list(read_refs),
                reanalysis_reason,
                reanalysis_head_ref,
            ),
        )
        row = self.conn.execute(
            "SELECT * FROM news_semantic_observations WHERE result_id = %s", (result_id,)
        ).fetchone()
        return dict(row)

    # ------------------------------------------------------------------ adoption
    def adopt_event_update(
        self,
        *,
        expected_head_ref: str | None,
        lease: SemanticLease,
        update: EventUpdate,
        document_json: str,
        observation_result_id: str,
        public_rows: Sequence[tuple[str, Mapping[str, Any]]],
        now_ms: int,
    ) -> bool:
        """CAS the head and write the update, its public outbox rows and the notification marker.

        Serialized per Event by a transaction advisory lock, so two adopters of one expected head can
        never both write: the second sees the first's head and returns False with nothing written.
        """

        event_id = update.event_id
        if event_id != lease.event_id or update.input_revision != lease.wanted_revision:
            raise EventUpdateConflict("news_semantic_update_lease_mismatch")
        lock_event(self.conn, event_id)
        self.require_semantic_owner(lease, now_ms=now_ms)
        try:
            return commit_update(
                self,
                expected_head_ref=expected_head_ref,
                update=update,
                document_json=document_json,
                source=SemanticSource(observation_result_id),
                public_rows=public_rows,
                now_ms=now_ms,
            )
        except ValueError as exc:
            raise EventUpdateConflict(str(exc)) from exc

    # ------------------------------------------------------------------ notification snapshot and plan
    def lookup_notification_decision(self, *, event_id: str, channel: str, input_digest: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            """SELECT plan FROM news_notification_decisions
                WHERE event_id = %s AND channel = %s AND input_digest = %s
                ORDER BY created_at_ms DESC, decision_ref DESC LIMIT 1""",
            (event_id, channel, input_digest),
        ).fetchone()
        return None if row is None else dict(row["plan"])

    def invalidated_claim_refs(self, event_id: str) -> list[str]:
        """Read the adopted change ledger; no second writable claim-status authority."""
        rows = self.conn.execute(
            """
            WITH own AS (
              SELECT ARRAY(SELECT jsonb_array_elements(u.document->'claims')->>'ref') AS refs
                FROM news_event_update_heads h JOIN news_event_updates u
                  ON u.event_id=h.event_id AND u.content_revision=h.content_revision
               WHERE h.event_id=%s
            )
            SELECT DISTINCT change->>'previous_ref' AS ref
              FROM own JOIN news_event_updates u
                ON jsonb_path_query_array(u.document, '$.changes[*].previous_ref') ?| own.refs
              CROSS JOIN LATERAL jsonb_array_elements(u.document->'changes') change
             WHERE change->>'previous_ref'=ANY(own.refs)
               AND change->>'relation' IN ('corrects','real_world_change')
            """,
            (event_id,),
        ).fetchall()
        return sorted(str(row["ref"]) for row in rows)

    def _unsettled_claim_refs(self, event_id: str) -> tuple[list[str], list[str]]:
        """This Event's claims in sends still in flight, and in sends whose outcome is ambiguous."""

        rows = self.conn.execute(
            """
            SELECT state, claim_refs FROM news_deliveries
             WHERE event_id = %s AND kind = 'update' AND state IN ('sending', 'ambiguous')
            """,
            (event_id,),
        ).fetchall()
        return (
            sorted({str(ref) for row in rows if row["state"] == "sending" for ref in row["claim_refs"] or ()}),
            sorted({str(ref) for row in rows if row["state"] == "ambiguous" for ref in row["claim_refs"] or ()}),
        )

    def _receipt_rows(self, event_ids: Sequence[str], *, until_ms: int | None) -> list[dict[str, Any]]:
        if not event_ids:
            return []
        return [
            dict(row)
            for row in self.conn.execute(
                f"""
                SELECT {_RECEIPT_COLUMNS} FROM news_deliveries d
                 WHERE d.event_id = ANY(%s) AND d.kind = 'update' AND d.state = 'sent'
                   AND d.delete_state IS DISTINCT FROM 'deleted'
                   AND (%s::bigint IS NULL OR d.settled_at_ms < %s)
                 ORDER BY d.settled_at_ms DESC, d.intent_id
                """,  # noqa: S608 - a module-owned column list
                (list(event_ids), until_ms, until_ms),
            ).fetchall()
        ]

    def _similar_receipt_rows(
        self, queries: Sequence[str], *, now_ms: int, until_ms: int | None
    ) -> list[dict[str, Any]]:
        """Receipts of any Event whose delivered text or titles are closest to this update's claims."""

        if not queries:
            return []
        return [
            dict(row)
            for row in self.conn.execute(
                f"""
                SELECT {_RECEIPT_COLUMNS}
                  FROM news_deliveries d
                  CROSS JOIN LATERAL (
                    SELECT max(GREATEST(
                        similarity(COALESCE(d.history_context ->> 'comparison_title', ''), q),
                        similarity(COALESCE(d.history_context ->> 'headline_zh', ''), q),
                        similarity(COALESCE(d.body, d.history_context ->> 'why_zh', ''), q)
                    )) AS score FROM unnest(%s::text[]) AS q
                  ) relevance
                 WHERE d.kind = 'update' AND d.state = 'sent'
                   AND d.delete_state IS DISTINCT FROM 'deleted'
                   AND d.settled_at_ms >= %s AND (%s::bigint IS NULL OR d.settled_at_ms < %s)
                   AND relevance.score > 0
                 ORDER BY relevance.score DESC, d.settled_at_ms DESC, d.intent_id
                 LIMIT %s
                """,  # noqa: S608 - a module-owned column list
                (list(queries), int(now_ms) - TARGETED_HISTORY_WINDOW_MS, until_ms, until_ms, SIMILAR_TITLE_MAX),
            ).fetchall()
        ]

    def _linked_receipt_rows(self, refs: Sequence[str], *, now_ms: int, until_ms: int | None) -> list[dict[str, Any]]:
        """Receipts of any Event that carried one of these claims or their antecedents."""

        if not refs:
            return []
        return [
            dict(row)
            for row in self.conn.execute(
                f"""
                SELECT {_RECEIPT_COLUMNS} FROM news_deliveries d
                 WHERE d.kind = 'update' AND d.state = 'sent'
                   AND d.delete_state IS DISTINCT FROM 'deleted'
                   AND d.settled_at_ms >= %s AND (%s::bigint IS NULL OR d.settled_at_ms < %s)
                   AND d.claim_refs ?| %s::text[]
                """,  # noqa: S608 - a module-owned column list
                (int(now_ms) - TARGETED_HISTORY_WINDOW_MS, until_ms, until_ms, list(refs)),
            ).fetchall()
        ]

    def _reader_state(
        self,
        *,
        event_id: str,
        head: EventUpdate,
        now_ms: int,
        watch_symbols: Iterable[str],
        until_ms: int | None,
    ) -> dict[str, Any]:
        """What this Event's reader revision is made of, read the same way by the snapshot and both CASes.

        The related receipts are the ones a reader of this Event can be said to have been told: its own,
        any linked to its claims, and the closest by text. A receipt that only happens to be recent is a
        candidate the planner may still compare, but it is not what makes a plan stale, so an unrelated
        send elsewhere never invalidates this one. The snapshot reads up to its own stamp; a CAS reads
        whatever has settled, so a related receipt that arrived after the snapshot is exactly what it sees.
        """

        sending, ambiguous = self._unsettled_claim_refs(event_id)
        invalidated = self.invalidated_claim_refs(event_id)
        event = self.conn.execute(
            "SELECT comparison_title, event_kind FROM news_events WHERE event_id = %s", (event_id,)
        ).fetchone()
        queries = receipt_queries(head, str(event["comparison_title"] or "") if event else "", invalidated)
        own = self._receipt_rows([event_id], until_ms=until_ms)
        similar = self._similar_receipt_rows(queries, now_ms=now_ms, until_ms=until_ms)
        linked = self._linked_receipt_rows(sorted(linked_refs(head, invalidated)), now_ms=now_ms, until_ms=until_ms)
        return {
            "event": None if event is None else dict(event),
            "queries": queries,
            "sending": sending,
            "ambiguous": ambiguous,
            "invalidated": invalidated,
            "own": own,
            "similar": similar,
            "revision": reader_revision(
                (*own, *similar, *linked),
                blocked_claim_refs=sending,
                ambiguous_claim_refs=ambiguous,
                watch_symbols=watch_symbols,
                invalidated_claim_refs=invalidated,
            ),
        }

    def _current_reader_revision(self, event_id: str, *, now_ms: int, watch_symbols: Iterable[str]) -> str | None:
        document = self.event_update_head_document(event_id)
        if document is None:
            return None
        head = EventUpdate.model_validate(document)
        state = self._reader_state(
            event_id=event_id, head=head, now_ms=now_ms, watch_symbols=watch_symbols, until_ms=None
        )
        return str(state["revision"])

    def notification_snapshot_material(
        self, *, event_id: str, channel: str, now_ms: int, watch_symbols: Iterable[str]
    ) -> dict[str, Any] | None:
        """The pending head, the receipts the planner may compare, and the related-receipt reader revision."""

        work = self.conn.execute(
            "SELECT content_revision, state, next_attempt_at_ms, updated_at_ms FROM news_notification_work "
            "WHERE event_id = %s AND channel = %s",
            (event_id, channel),
        ).fetchone()
        if work is None or work["state"] != "pending":
            return None
        head = self.event_update_head_document(event_id)
        if head is None or head.get("content_revision") != work["content_revision"]:
            return None
        reader = self._reader_state(
            event_id=event_id,
            head=EventUpdate.model_validate(head),
            now_ms=now_ms,
            watch_symbols=watch_symbols,
            until_ms=now_ms,
        )
        # The history bands use the leader. Recall content from later members directly at receipt
        # granularity too; rank all bands again outside this read before the final model budget.
        history = cast(DecisionStorage, self).reader_history(event_id=event_id, now_ms=now_ms)
        band = self._receipt_rows(
            [row.event_id for row in history.told_source_rows if row.event_id != event_id], until_ms=now_ms
        )
        event = reader["event"]
        listing_members = (
            self.conn.execute(
                """SELECT m.item_id,m.fact_text,i.provider_metadata
                     FROM news_event_members m JOIN news_items i ON i.item_id=m.item_id
                    WHERE m.event_id=%s""",
                (event_id,),
            ).fetchall()
            if event is not None and event["event_kind"] == "listing"
            else []
        )
        return {
            "work_updated_at_ms": int(work["updated_at_ms"]),
            "work_due_at_ms": int(work["next_attempt_at_ms"]),
            "head": head,
            "blocked": reader["sending"],
            "ambiguous": reader["ambiguous"],
            "invalidated": reader["invalidated"],
            "revision": reader["revision"],
            "receipt_queries": reader["queries"],
            "receipt_rows": [*reader["own"], *band, *reader["similar"]],
            "listing_members": [dict(row) for row in listing_members],
        }

    def _record_decision(
        self, event_id: str, plan: NotificationPlan, plan_json: str, *, now_ms: int
    ) -> NotificationPlan:
        """Insert-only decision; an identical plan is the same row, and the stored one wins."""

        self.conn.execute(
            """
            INSERT INTO news_notification_decisions
              (decision_ref,event_id,update_ref,channel,input_digest,input_snapshot,plan,origin,created_at_ms)
            VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,'editorial_v1',%s)
            ON CONFLICT DO NOTHING
            """,
            (
                plan.record_ref,
                event_id,
                plan.update_ref,
                plan.channel,
                plan.assessment_input_digest,
                _dumps(plan.assessment_input or {}),
                plan_json,
                int(now_ms),
            ),
        )
        stored = self.conn.execute(
            "SELECT decision_ref,plan FROM news_notification_decisions WHERE decision_ref = %s",
            (plan.record_ref,),
        ).fetchone()
        if stored is None:
            raise EventUpdateConflict("news_notification_decision_missing")
        return NotificationPlan.model_validate(stored["plan"]).model_copy(
            update={"reader_revision": plan.reader_revision, "decision_ref": str(stored["decision_ref"])}
        )

    def record_notification_plan(
        self,
        *,
        plan: NotificationPlan,
        plan_json: str,
        lease_token: str,
        watch_symbols: Iterable[str],
        now_ms: int,
        lease_ms: int = INTENT_LEASE_MS,
    ) -> dict[str, Any]:
        """Record or reuse an immutable decision, CAS head/reader, then reserve its stable intent."""

        head = self.conn.execute(
            "SELECT event_id, content_revision FROM news_event_update_heads WHERE update_ref = %s",
            (plan.update_ref,),
        ).fetchone()
        if head is None:
            return {"status": "head_changed"}
        event_id = str(head["event_id"])
        lock_event(self.conn, event_id)
        head = self.conn.execute(
            "SELECT event_id,content_revision FROM news_event_update_heads WHERE event_id=%s AND update_ref=%s",
            (event_id, plan.update_ref),
        ).fetchone()
        if head is None:
            return {"status": "head_changed"}
        work = self.conn.execute(
            """
            SELECT state, content_revision, attempts FROM news_notification_work
             WHERE event_id = %s AND channel = %s FOR UPDATE
            """,
            (event_id, plan.channel),
        ).fetchone()
        if work is None or work["content_revision"] != head["content_revision"]:
            return {"status": "head_changed"}
        if work["state"] != "pending":
            return {"status": "already_settled"}
        # The judgment is kept before the reader is checked: a plan that loses the race to a related receipt
        # is asked again with its assessment reused rather than asked from scratch (#742 N10).
        plan = self._record_decision(event_id, plan, plan_json, now_ms=now_ms)
        if self._current_reader_revision(event_id, now_ms=now_ms, watch_symbols=watch_symbols) != plan.reader_revision:
            return {"status": "reader_changed"}
        intent_id = plan.intent_id if plan.action == "notify" else None
        others = self.conn.execute(
            """
            SELECT q.intent_id, q.lease_token, q.next_attempt_at_ms
              FROM news_delivery_queue q
             WHERE q.event_id = %s AND q.kind = 'update' AND q.state = 'pending'
               AND q.intent_id IS DISTINCT FROM %s
               AND NOT EXISTS (SELECT 1 FROM news_deliveries d WHERE d.intent_id = q.intent_id)
             FOR UPDATE OF q
            """,
            (event_id, intent_id),
        ).fetchall()
        leased = [
            int(row["next_attempt_at_ms"])
            for row in others
            if row["lease_token"] is not None and int(row["next_attempt_at_ms"]) > int(now_ms)
        ]
        if leased:
            # Another intent of this Event is still owned. Wait for it rather than poll it: its end wakes this.
            self._postpone_work(event_id, next_at_ms=min(leased))
            return {"status": "overlap"}
        if others:
            # Superseded unsent reservations of this Event: retire them, never a frozen send.
            self.conn.execute(
                "DELETE FROM news_delivery_queue WHERE intent_id = ANY(%s)",
                ([str(row["intent_id"]) for row in others],),
            )

        def recorded(intent: str | None = None, card: Any = None) -> dict[str, Any]:
            return {
                "status": "committed",
                "plan": plan.model_dump(mode="json"),
                "intent_id": intent,
                "frozen_card": card,
            }

        attempts = int(work["attempts"])
        if plan.action == "no_notification":
            self._settle_work(event_id, plan, state="done", attempts=0, next_at_ms=now_ms, now_ms=now_ms)
            return recorded()
        if plan.action == "unresolved":
            self._wait_work(event_id, plan, attempts=attempts, now_ms=now_ms)
            return recorded()
        intent_id = plan.intent_id
        ledger = self.conn.execute("SELECT state FROM news_deliveries WHERE intent_id = %s", (intent_id,)).fetchone()
        if ledger is not None:
            if ledger["state"] == "sending":
                self._wait_work(event_id, plan, attempts=attempts, now_ms=now_ms)
            else:
                # This exact selection already reached its final outcome: the plan is recorded, not resent.
                self._complete_plan(event_id, plan, attempts=attempts, now_ms=now_ms)
            return {"status": "already_settled"}
        selected = list(plan.selected_claim_refs)
        reserved = self.conn.execute(
            """
            INSERT INTO news_delivery_queue (
              intent_id, event_id, kind, state, attempts, enqueued_at_ms, next_attempt_at_ms,
              last_attempt_at_ms, updated_at_ms, content_revision, claim_refs, plan_key, lease_token, decision_ref
            ) VALUES (%s, %s, 'update', 'pending', 0, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s)
            ON CONFLICT (intent_id) DO NOTHING
            RETURNING frozen_card
            """,
            (
                intent_id,
                event_id,
                int(now_ms),
                int(now_ms) + int(lease_ms),
                int(now_ms),
                int(now_ms),
                head["content_revision"],
                _dumps(selected),
                plan.key,
                lease_token,
                plan.record_ref,
            ),
        ).fetchone()
        frozen_card = None
        if reserved is None:
            existing = self.conn.execute(
                """
                SELECT state, lease_token, next_attempt_at_ms, frozen_card, error_code
                  FROM news_delivery_queue WHERE intent_id = %s FOR UPDATE
                """,
                (intent_id,),
            ).fetchone()
            if existing["state"] == "dead":
                # This selection already failed as an unsent intent; only an explicit retry revives it.
                self._settle_work(
                    event_id,
                    plan,
                    state="failed",
                    attempts=attempts,
                    next_at_ms=now_ms,
                    now_ms=now_ms,
                    error_code=str(existing["error_code"] or "news_delivery_intent_dead"),
                )
                return {"status": "already_settled"}
            if existing["lease_token"] is not None and int(existing["next_attempt_at_ms"]) > int(now_ms):
                return {"status": "overlap"}
            # A re-lease sends under the decision just recorded, not the one that first reserved the intent.
            self.conn.execute(
                """
                UPDATE news_delivery_queue
                   SET lease_token = %s, next_attempt_at_ms = %s, last_attempt_at_ms = %s, updated_at_ms = %s,
                       decision_ref = %s, plan_key = %s
                 WHERE intent_id = %s
                """,
                (
                    lease_token,
                    int(now_ms) + int(lease_ms),
                    int(now_ms),
                    int(now_ms),
                    plan.record_ref,
                    plan.key,
                    intent_id,
                ),
            )
            frozen_card = existing["frozen_card"]
        # The marker stays pending while the reserved intent is in flight. If this turn dies before
        # the send is settled, the marker comes due after the lease and reclaims the same identity.
        self._settle_work(
            event_id, plan, state="pending", attempts=attempts, next_at_ms=int(now_ms) + int(lease_ms), now_ms=now_ms
        )
        return recorded(intent_id, frozen_card)

    def _complete_plan(self, event_id: str, plan: NotificationPlan, *, attempts: int, now_ms: int) -> None:
        # Deferred claims keep the marker waiting for a later turn; otherwise this head is planned.
        if plan.deferred_claim_refs:
            self._wait_work(event_id, plan, attempts=attempts, now_ms=now_ms)
        else:
            self._settle_work(event_id, plan, state="done", attempts=0, next_at_ms=now_ms, now_ms=now_ms)

    def _intent_ended(
        self,
        event_id: str,
        content_revision: str,
        decision_ref: str | None,
        *,
        now_ms: int,
        error_code: str | None = None,
    ) -> None:
        """An intent reached its end: it completes -- or, with an error code, fails -- the plan it was sent for.

        Only the work's current decision is completed by its own intent. Any other intent ending wakes the
        Event's pending work instead, which may be a newer head waiting for exactly this send to finish.
        """

        work = self.conn.execute(
            """
            SELECT w.attempts, w.state, w.content_revision, w.decision_ref, d.plan
              FROM news_notification_work w
              LEFT JOIN news_notification_decisions d ON d.decision_ref = w.decision_ref
             WHERE w.event_id = %s AND w.channel = %s FOR UPDATE OF w
            """,
            (event_id, NEWS_CHANNEL),
        ).fetchone()
        if work is None or work["state"] != "pending":
            return
        if work["content_revision"] != content_revision or work["plan"] is None or work["decision_ref"] != decision_ref:
            self._wake_work(event_id, now_ms=now_ms)
            return
        plan = NotificationPlan.model_validate(work["plan"]).model_copy(
            update={"decision_ref": str(work["decision_ref"])}
        )
        if error_code is None:
            self._complete_plan(event_id, plan, attempts=int(work["attempts"]), now_ms=now_ms)
        else:
            self._settle_work(
                event_id,
                plan,
                state="failed",
                attempts=int(work["attempts"]),
                next_at_ms=now_ms,
                now_ms=now_ms,
                error_code=error_code,
            )

    def _wait_work(self, event_id: str, plan: NotificationPlan, *, attempts: int, now_ms: int) -> None:
        """Only a send of this Event still in flight is waited on. Waiting spends no attempt.

        It is bounded by that send: its settlement wakes this work, and a send whose owner is gone is
        held ambiguous by the reconciliation within `SENDING_ORPHAN_MS`, after which nothing waits on it.
        """

        self._settle_work(
            event_id,
            plan,
            state="pending",
            attempts=attempts,
            next_at_ms=int(now_ms) + NOTIFICATION_WAIT_MS,
            now_ms=now_ms,
        )

    def _settle_work(
        self,
        event_id: str,
        plan: NotificationPlan,
        *,
        state: str,
        attempts: int,
        next_at_ms: int,
        now_ms: int,
        error_code: str | None = None,
    ) -> None:
        self.conn.execute(
            """
            UPDATE news_notification_work
               SET state = %s, decision_ref = %s, reader_revision = %s, attempts = %s,
                   last_error_code = CASE WHEN %s = 'done' THEN NULL ELSE COALESCE(%s, last_error_code) END,
                   next_attempt_at_ms = %s, updated_at_ms = %s
             WHERE event_id = %s AND channel = %s
            """,
            (
                state,
                plan.record_ref,
                plan.reader_revision,
                attempts,
                state,
                error_code,
                int(next_at_ms),
                int(now_ms),
                event_id,
                plan.channel,
            ),
        )

    def _postpone_work(self, event_id: str, *, next_at_ms: int) -> None:
        """Move the Event's pending work to `next_at_ms` without touching its plan, attempts or CAS stamp."""

        self.conn.execute(
            "UPDATE news_notification_work SET next_attempt_at_ms = %s "
            "WHERE event_id = %s AND channel = %s AND state = 'pending'",
            (int(next_at_ms), event_id, NEWS_CHANNEL),
        )

    def _wake_work(self, event_id: str, *, now_ms: int) -> None:
        """Make the Event's pending work due now, if it was waiting; nothing else about it changes."""

        self.conn.execute(
            "UPDATE news_notification_work SET next_attempt_at_ms = %s "
            "WHERE event_id = %s AND channel = %s AND state = 'pending' AND next_attempt_at_ms > %s",
            (int(now_ms), event_id, NEWS_CHANNEL, int(now_ms)),
        )

    def _pend_notification(
        self, event_id: str, *, expected_content_revision: str, next_at_ms: int, now_ms: int
    ) -> None:
        """An intent of this revision is owed again at `next_at_ms`; a newer head's work is woken now."""

        updated = self.conn.execute(
            """
            UPDATE news_notification_work SET state = 'pending', next_attempt_at_ms = %s, updated_at_ms = %s
             WHERE event_id = %s AND channel = %s AND content_revision = %s AND state <> 'failed'
            """,
            (int(next_at_ms), int(now_ms), event_id, NEWS_CHANNEL, expected_content_revision),
        )
        if not updated.rowcount:
            self._wake_work(event_id, now_ms=now_ms)

    # ------------------------------------------------------------------ intent card, send and settlement
    def lookup_card_copy(self, *, input_digest: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            """SELECT card_copy_document FROM (
                SELECT card_copy_document, attempted_at_ms AS stamp FROM news_deliveries
                 WHERE kind='update' AND card_copy_input_digest=%s AND card_copy_document IS NOT NULL
                UNION ALL
                SELECT card_copy_document, updated_at_ms AS stamp FROM news_delivery_queue
                 WHERE kind='update' AND card_copy_input_digest=%s AND card_copy_document IS NOT NULL
                ) copies ORDER BY stamp DESC LIMIT 1""",
            (input_digest, input_digest),
        ).fetchone()
        return None if row is None else dict(row["card_copy_document"])

    def save_intent_card(
        self, *, intent_id: str, lease_token: str, card_json: str, copy_json: str, input_digest: str, now_ms: int
    ) -> dict[str, Any]:
        """Fenced insert-only frozen payload: an existing frozen card wins and is returned."""

        self.conn.execute(
            """
            UPDATE news_delivery_queue
               SET frozen_card = %s::jsonb, card_copy_document=%s::jsonb,
                   card_copy_input_digest=%s, updated_at_ms = %s
             WHERE intent_id = %s AND kind = 'update' AND state = 'pending'
               AND lease_token = %s AND frozen_card IS NULL
            """,
            (card_json, copy_json, input_digest, int(now_ms), intent_id, lease_token),
        )
        row = self.conn.execute(
            "SELECT lease_token, state, frozen_card FROM news_delivery_queue WHERE intent_id = %s", (intent_id,)
        ).fetchone()
        if row is None or row["state"] != "pending" or row["lease_token"] != lease_token or row["frozen_card"] is None:
            raise IntentLeaseLost("news_intent_lease_lost")
        return dict(row["frozen_card"])

    def release_unsent_intent(self, *, intent_id: str, lease_token: str, now_ms: int) -> bool:
        identity_row = self.conn.execute(
            "SELECT event_id FROM news_delivery_queue WHERE intent_id=%s AND kind='update'", (intent_id,)
        ).fetchone()
        if identity_row is None:
            return False
        lock_event(self.conn, str(identity_row["event_id"]))
        row = self.conn.execute(
            """UPDATE news_delivery_queue SET lease_token=NULL, next_attempt_at_ms=%s, updated_at_ms=%s
                WHERE intent_id=%s AND kind='update' AND state='pending' AND lease_token=%s
                RETURNING event_id,content_revision""",
            (int(now_ms), int(now_ms), intent_id, lease_token),
        ).fetchone()
        if row is None:
            return False
        self._pend_notification(
            str(row["event_id"]),
            expected_content_revision=str(row["content_revision"]),
            next_at_ms=now_ms,
            now_ms=now_ms,
        )
        return True

    def begin_intent_send(
        self,
        *,
        intent_id: str,
        lease_token: str,
        plan: NotificationPlan,
        card: FrozenCard,
        watch_symbols: Iterable[str],
        now_ms: int,
        timings_json: str | None = None,
    ) -> BeginSendStatus:
        """Recheck head, reader revision, lease and in-flight overlap, then freeze `sending`.

        A changed head, related reader or overlapping send releases the unsent reservation (its frozen card
        is kept for the same identity) and leaves notification pending. An existing ledger row is never
        touched. The send's timings are kept beside its reader-history context.
        """

        identity_row = self.conn.execute(
            "SELECT event_id FROM news_delivery_queue WHERE intent_id=%s AND kind='update'", (intent_id,)
        ).fetchone()
        if identity_row is None:
            return "lease_lost"
        lock_event(self.conn, str(identity_row["event_id"]))
        queued = self.conn.execute(
            """
            SELECT event_id, state, lease_token, frozen_card, content_revision, claim_refs, plan_key,
                   decision_ref, card_copy_input_digest, card_copy_document
              FROM news_delivery_queue WHERE intent_id = %s AND kind = 'update' FOR UPDATE
            """,
            (intent_id,),
        ).fetchone()
        if queued is None or queued["state"] != "pending" or queued["lease_token"] != lease_token:
            return "lease_lost"
        if queued["decision_ref"] != plan.record_ref:
            raise EventUpdateConflict("news_intent_decision_mismatch")
        frozen = queued["frozen_card"]
        if frozen is None or FrozenCard.model_validate(frozen) != card:
            raise EventUpdateConflict("news_intent_card_not_frozen")
        if self.conn.execute("SELECT 1 FROM news_deliveries WHERE intent_id = %s", (intent_id,)).fetchone():
            return "already_settled"
        event_id = str(queued["event_id"])
        head = self.conn.execute(
            "SELECT update_ref FROM news_event_update_heads WHERE event_id = %s", (event_id,)
        ).fetchone()
        head_changed = head is None or head["update_ref"] != plan.update_ref
        reader_changed = (
            not head_changed
            and self._current_reader_revision(event_id, now_ms=now_ms, watch_symbols=watch_symbols)
            != plan.reader_revision
        )
        overlap = self.conn.execute(
            """
            SELECT 1 FROM news_deliveries
             WHERE event_id = %s AND kind = 'update' AND state IN ('sending', 'ambiguous')
               AND claim_refs ?| %s::text[]
             LIMIT 1
            """,
            (event_id, list(card.claim_refs)),
        ).fetchone()
        if head_changed or reader_changed or overlap is not None:
            self.conn.execute(
                """
                UPDATE news_delivery_queue SET lease_token = NULL, next_attempt_at_ms = %s, updated_at_ms = %s
                 WHERE intent_id = %s
                """,
                (int(now_ms), int(now_ms), intent_id),
            )
            self._pend_notification(
                event_id, expected_content_revision=str(queued["content_revision"]), next_at_ms=now_ms, now_ms=now_ms
            )
            return "head_changed" if head_changed else "reader_changed" if reader_changed else "overlap"
        inserted = self.conn.execute(
            """
            WITH selected AS (
              SELECT DISTINCT upper(asset ->> 'symbol') AS symbol
                FROM news_event_updates u
                CROSS JOIN LATERAL jsonb_array_elements(u.document -> 'claims') claim
                CROSS JOIN LATERAL jsonb_array_elements(claim #> '{fields,assets}') asset
               WHERE u.event_id = %(event)s AND u.content_revision = %(revision)s
                 AND claim ->> 'ref' = ANY(%(refs)s) AND asset ->> 'role' = 'primary'
            ), canonical AS (
              SELECT COALESCE(jsonb_agg(symbol ORDER BY symbol), '[]'::jsonb) AS symbols
                FROM (SELECT DISTINCT COALESCE(a.base_symbol, s.symbol) AS symbol
                        FROM selected s LEFT JOIN news_symbol_aliases a ON a.alias = s.symbol) resolved
            )
            INSERT INTO news_deliveries (
              intent_id, event_id, kind, state, card, attempted_at_ms, created_at_ms,
              content_revision, claim_refs, body, payload_sha256, plan_key, decision_ref, history_context,
              card_copy_input_digest, card_copy_document
            )
            SELECT %(intent)s, e.event_id, 'update', 'sending', %(card)s::jsonb, %(now)s, %(now)s,
                   %(revision)s, %(claim_refs)s::jsonb, %(body)s, %(sha)s, %(key)s, %(decision)s,
                   jsonb_build_object(
                     'event_id', e.event_id,
                     'intent_id', %(intent)s::text,
                     'headline_zh', %(headline)s::text,
                     'comparison_title', e.comparison_title,
                     'comparison_fingerprint', e.comparison_fingerprint,
                     'dedupe_family', e.dedupe_family,
                     'storyline_key', e.storyline_key,
                     'canonical_assets', canonical.symbols,
                     'timings', %(timings)s::jsonb),
                   %(copy_digest)s, %(copy_document)s::jsonb
              FROM news_events e CROSS JOIN canonical
             WHERE e.event_id = %(event)s
            ON CONFLICT (intent_id) DO NOTHING
            RETURNING state
            """,
            {
                "intent": intent_id,
                "event": event_id,
                "revision": queued["content_revision"],
                "refs": list(card.claim_refs),
                "card": _dumps(card.model_dump(mode="json")),
                "now": int(now_ms),
                "claim_refs": _dumps(list(queued["claim_refs"])),
                "body": card.body,
                "sha": card.payload_sha256,
                "key": bool(queued["plan_key"]),
                "decision": queued["decision_ref"],
                "headline": card.headline_zh,
                "timings": timings_json,
                "copy_digest": queued["card_copy_input_digest"],
                "copy_document": _dumps(queued["card_copy_document"]),
            },
        ).fetchone()
        return "begun" if inserted is not None else "already_settled"

    def settle_intent_send(
        self,
        *,
        intent_id: str,
        lease_token: str,
        payload_sha256: str,
        state: IntentOutcome,
        provider_message_id: str | None,
        error_code: str | None,
        retryable: bool,
        retry_after_ms: int | None,
        settled_at_ms: int,
        provider_receipt: Mapping[str, Any] | None = None,
    ) -> str:
        """Record the actual outcome of one frozen send or verify an identical prior settlement.

        Only a `sending` row with this exact payload is settled. Sent keeps the body, digest, provider
        message id and the provider's own receipt (what an in-place edit is later fenced by); a provider
        that answers with no message id is recorded with none. Ambiguous is held, and its claims count as
        possibly sent. A retryable not-sent never reached a reader, so its `sending` row is removed and the
        identity is released for the same payload under the queue's attempt bound and the provider's own
        `Retry-After`; the last one ends the unsent intent and fails the work. A refused one is terminal.
        """

        identity_row = self.conn.execute(
            """SELECT event_id FROM news_deliveries WHERE intent_id=%s
               UNION ALL SELECT event_id FROM news_delivery_queue WHERE intent_id=%s LIMIT 1""",
            (intent_id, intent_id),
        ).fetchone()
        if identity_row is None:
            return "conflict"
        lock_event(self.conn, str(identity_row["event_id"]))
        expected = {
            "lease_token": lease_token,
            "payload_sha256": payload_sha256,
            "state": state,
            "error_code": error_code,
            "retryable": retryable,
            "retry_after_ms": retry_after_ms,
            "provider_message_id": provider_message_id,
            "provider_receipt": dict(provider_receipt or {}),
        }
        receipt = {
            "channel": NEWS_CHANNEL,
            "payload_sha256": payload_sha256,
            "provider_message_id": provider_message_id,
            "pushed_at_ms": int(settled_at_ms),
            # The provider's own push stamp and target identity fence enrichment edits.
            **dict(provider_receipt or {}),
        }
        ledger = self.conn.execute(
            """
            SELECT event_id, state, payload_sha256, content_revision, decision_ref, settlement FROM news_deliveries
             WHERE intent_id = %s FOR UPDATE
            """,
            (intent_id,),
        ).fetchone()
        if ledger is None:
            previous = self.conn.execute(
                "SELECT last_settlement FROM news_delivery_queue WHERE intent_id=%s FOR UPDATE", (intent_id,)
            ).fetchone()
            return "already_settled" if previous and previous["last_settlement"] == expected else "conflict"
        if ledger["payload_sha256"] != payload_sha256:
            return "conflict"
        if ledger["state"] != "sending":
            return "already_settled" if ledger["settlement"] == expected else "conflict"
        event_id = str(ledger["event_id"])
        content_revision = str(ledger["content_revision"])
        decision_ref = ledger["decision_ref"]
        queued = self.conn.execute(
            "SELECT attempts, lease_token, last_settlement FROM news_delivery_queue WHERE intent_id = %s FOR UPDATE",
            (intent_id,),
        ).fetchone()
        if queued is None or queued["lease_token"] != lease_token:
            previous = None if queued is None else queued["last_settlement"]
            return "already_settled" if previous == expected else "conflict"
        now_ms = int(settled_at_ms)
        if state == "sent":
            self.conn.execute(
                """
                UPDATE news_deliveries SET state = 'sent', receipt = %s::jsonb, settlement = %s::jsonb,
                       error_code = NULL, settled_at_ms = %s
                 WHERE intent_id = %s
                """,
                (_dumps(receipt), _dumps(expected), now_ms, intent_id),
            )
            self.conn.execute("DELETE FROM news_delivery_queue WHERE intent_id = %s", (intent_id,))
            self._intent_ended(event_id, content_revision, decision_ref, now_ms=now_ms)
            return "sent"
        if state == "ambiguous":
            self.conn.execute(
                """
                UPDATE news_deliveries SET state = 'ambiguous', error_code = %s,
                       settlement = %s::jsonb, settled_at_ms = %s
                 WHERE intent_id = %s
                """,
                (error_code or "send_outcome_ambiguous", _dumps(expected), now_ms, intent_id),
            )
            self.conn.execute("DELETE FROM news_delivery_queue WHERE intent_id = %s", (intent_id,))
            self._intent_ended(event_id, content_revision, decision_ref, now_ms=now_ms)
            return "ambiguous"
        code = error_code or "send_not_sent"
        if retryable:
            self.conn.execute("DELETE FROM news_deliveries WHERE intent_id = %s AND state = 'sending'", (intent_id,))
            self._fail_unsent_intent(
                intent_id,
                error_code=code,
                retryable=True,
                retry_after_ms=retry_after_ms,
                now_ms=now_ms,
                settlement=expected,
            )
            return "not_sent"
        self.conn.execute(
            """
            UPDATE news_deliveries SET state = 'terminal', error_code = %s,
                   settlement = %s::jsonb, settled_at_ms = %s
             WHERE intent_id = %s
            """,
            (code, _dumps(expected), now_ms, intent_id),
        )
        self.conn.execute(
            """
            UPDATE news_delivery_queue
               SET state = 'dead', attempts = LEAST(attempts + 1, %s), lease_token = NULL,
                   error_code = %s, settled_at_ms = %s, updated_at_ms = %s,
                   last_settlement = %s::jsonb
             WHERE intent_id = %s
            """,
            (INTENT_ATTEMPTS_MAX, code, now_ms, now_ms, _dumps(expected), intent_id),
        )
        self._intent_ended(event_id, content_revision, decision_ref, now_ms=now_ms)
        return "terminal"

    def _fail_unsent_intent(
        self,
        intent_id: str,
        *,
        error_code: str,
        retryable: bool,
        retry_after_ms: int | None,
        now_ms: int,
        lease_token: str | None = None,
        settlement: Mapping[str, Any] | None = None,
    ) -> bool:
        """One failure of an owned intent that never reached a reader: back off, or end it and fail the work."""

        row = self.conn.execute(
            """
            UPDATE news_delivery_queue
               SET attempts = CASE WHEN %(retryable)s THEN LEAST(attempts + 1, %(max)s) ELSE %(max)s END,
                   state = CASE WHEN %(retryable)s AND attempts + 1 < %(max)s THEN 'pending' ELSE 'dead' END,
                   settled_at_ms = CASE WHEN %(retryable)s AND attempts + 1 < %(max)s
                                        THEN NULL ELSE %(now)s::bigint END,
                   next_attempt_at_ms = %(now)s::bigint + GREATEST(
                     (%(delays)s::bigint[])[GREATEST(1, LEAST(attempts + 1, %(delay_n)s))], %(retry_after)s::bigint),
                   lease_token = NULL, error_code = %(code)s, updated_at_ms = %(now)s,
                   last_settlement = COALESCE(%(settlement)s::jsonb, last_settlement)
             WHERE intent_id = %(intent)s AND kind = 'update' AND state = 'pending'
               AND (%(lease)s::text IS NULL OR lease_token = %(lease)s)
            RETURNING event_id, state, next_attempt_at_ms, content_revision, decision_ref
            """,
            {
                "retryable": bool(retryable),
                "max": INTENT_ATTEMPTS_MAX,
                "now": int(now_ms),
                "delays": list(INTENT_RETRY_MS),
                "delay_n": len(INTENT_RETRY_MS),
                "retry_after": int(retry_after_ms or 0),
                "code": error_code,
                "settlement": None if settlement is None else _dumps(settlement),
                "intent": intent_id,
                "lease": lease_token,
            },
        ).fetchone()
        if row is None:
            return False
        if row["state"] == "pending":
            self._pend_notification(
                str(row["event_id"]),
                expected_content_revision=str(row["content_revision"]),
                next_at_ms=int(row["next_attempt_at_ms"]),
                now_ms=now_ms,
            )
        else:
            self._intent_ended(
                str(row["event_id"]),
                str(row["content_revision"]),
                row["decision_ref"],
                now_ms=now_ms,
                error_code=error_code,
            )
        return True

    def record_unsent_intent_failure(
        self,
        *,
        intent_id: str,
        lease_token: str,
        error_code: str,
        retryable: bool,
        retry_after_ms: int | None,
        now_ms: int,
    ) -> bool:
        """A card failure or a proven unsent preflight: fenced by the lease, never through a `sending` row."""

        identity_row = self.conn.execute(
            "SELECT event_id FROM news_delivery_queue WHERE intent_id=%s AND kind='update'", (intent_id,)
        ).fetchone()
        if identity_row is None:
            return False
        lock_event(self.conn, str(identity_row["event_id"]))
        return self._fail_unsent_intent(
            intent_id,
            error_code=error_code,
            retryable=retryable,
            retry_after_ms=retry_after_ms,
            now_ms=now_ms,
            lease_token=lease_token,
        )

    def terminalize_interrupted_deliveries(
        self, *, now_ms: int, exclude_intent_ids: Sequence[str] = (), limit: int = ORPHAN_SEND_BATCH_MAX
    ) -> int:
        """Hold ambiguous every `sending` row whose owner is gone, and complete the plan it was sent for.

        An owner outlives neither its provider call nor its bounded settlement, so a row older than
        `SENDING_ORPHAN_MS` has none -- except the sends this process says it still holds. Run at start and
        periodically, so an orphan never outlives a restart or keeps its claims waiting.
        """

        rows = self.conn.execute(
            """
            SELECT intent_id, event_id FROM news_deliveries
             WHERE kind = 'update' AND state = 'sending' AND attempted_at_ms < %s
               AND NOT (intent_id = ANY(%s::text[]))
             ORDER BY event_id, intent_id
             LIMIT %s
            """,
            (int(now_ms) - SENDING_ORPHAN_MS, list(exclude_intent_ids), int(limit)),
        ).fetchall()
        settled = 0
        for candidate in rows:
            lock_event(self.conn, str(candidate["event_id"]))
            row = self.conn.execute(
                """
                UPDATE news_deliveries SET state = 'ambiguous', error_code = 'ambiguous_after_crash', settled_at_ms = %s
                 WHERE intent_id = %s AND state = 'sending'
                RETURNING event_id, content_revision, decision_ref
                """,
                (int(now_ms), candidate["intent_id"]),
            ).fetchone()
            if row is None:
                continue
            self.conn.execute("DELETE FROM news_delivery_queue WHERE intent_id = %s", (candidate["intent_id"],))
            self._intent_ended(str(row["event_id"]), str(row["content_revision"]), row["decision_ref"], now_ms=now_ms)
            settled += 1
        return settled

    def defer_notification_work(
        self,
        *,
        event_id: str,
        channel: str,
        expected_content_revision: str | None,
        expected_work_updated_at_ms: int | None = None,
        error_code: str,
        now_ms: int,
    ) -> bool:
        """Spend one attempt of the failed snapshot's work; the last one fails it. A superseded turn is a no-op."""

        lock_event(self.conn, event_id)
        cursor = self.conn.execute(
            """
            UPDATE news_notification_work
               SET attempts = LEAST(attempts + 1, %(max)s),
                   state = CASE WHEN attempts + 1 >= %(max)s THEN 'failed' ELSE 'pending' END,
                   last_error_code = %(code)s,
                   next_attempt_at_ms = CASE WHEN attempts + 1 >= %(max)s THEN next_attempt_at_ms
                     ELSE %(now)s::bigint + (%(delays)s::bigint[])[attempts + 1] END,
                   updated_at_ms = GREATEST(%(now)s, updated_at_ms + 1)
             WHERE event_id = %(event)s AND channel = %(channel)s AND state = 'pending'
               AND (%(revision)s::text IS NULL OR content_revision = %(revision)s)
               AND (%(updated)s::bigint IS NULL OR updated_at_ms = %(updated)s)
            """,
            {
                "max": NOTIFICATION_ATTEMPTS_MAX,
                "code": error_code,
                "now": int(now_ms),
                "delays": list(NOTIFICATION_RETRY_MS),
                "event": event_id,
                "channel": channel,
                "revision": expected_content_revision,
                "updated": expected_work_updated_at_ms,
            },
        )
        return bool(cursor.rowcount)

    def postpone_notification_work(
        self, *, event_id: str, channel: str, expected_content_revision: str | None, now_ms: int
    ) -> bool:
        """Put pending work off by one wait without spending an attempt or moving its CAS stamp."""

        lock_event(self.conn, event_id)
        cursor = self.conn.execute(
            """
            UPDATE news_notification_work SET next_attempt_at_ms = GREATEST(next_attempt_at_ms, %s)
             WHERE event_id = %s AND channel = %s AND state = 'pending'
               AND (%s::text IS NULL OR content_revision = %s)
            """,
            (
                int(now_ms) + NOTIFICATION_WAIT_MS,
                event_id,
                channel,
                expected_content_revision,
                expected_content_revision,
            ),
        )
        return bool(cursor.rowcount)

    def pending_notification_event_ids(self, *, channel: str, now_ms: int, limit: int) -> list[str]:
        rows = self.conn.execute(
            """
            SELECT event_id FROM news_notification_work
             WHERE channel = %s AND state = 'pending' AND next_attempt_at_ms <= %s
             ORDER BY next_attempt_at_ms, event_id
             LIMIT %s
            """,
            (channel, int(now_ms), int(limit)),
        ).fetchall()
        return [str(row["event_id"]) for row in rows]

    def retry_failed_work(self, *, event_id: str, kind: str, revision: str, now_ms: int) -> bool:
        """Explicit operator recovery of one failed version, retaining facts, checkpoints and receipts.

        Notification recovery reopens failed work of that exact content revision and revives its unsent
        intents that failed. A ledger row (including a terminal or ambiguous send) is never erased or
        reopened by this operation.
        """

        if kind not in {"semantic", "notification"}:
            raise ValueError("news_retry_work_target_invalid")
        if kind == "semantic":
            wanted = int(revision)
            if wanted < 1:
                raise ValueError("news_retry_work_revision_invalid")
            cursor = self.conn.execute(
                """
                UPDATE news_semantic_work
                   SET attempts = 0, last_outcome = NULL, lease_token = NULL, leased_until_ms = NULL,
                       failed_read_refs = '{}', next_attempt_at_ms = %s, published_at_ms = NULL, updated_at_ms = %s
                 WHERE event_id = %s AND wanted_revision = %s AND last_outcome = 'failed'
                   AND (done_revision IS NULL OR done_revision < wanted_revision)
                   AND (leased_until_ms IS NULL OR leased_until_ms <= %s)
                """,
                (int(now_ms), int(now_ms), event_id, wanted, int(now_ms)),
            )
            return bool(cursor.rowcount)
        lock_event(self.conn, event_id)
        cursor = self.conn.execute(
            """
            UPDATE news_notification_work
               SET state = 'pending', attempts = 0, next_attempt_at_ms = %s, updated_at_ms = %s
             WHERE event_id = %s AND channel = %s AND content_revision = %s AND state = 'failed'
            """,
            (int(now_ms), int(now_ms), event_id, NEWS_CHANNEL, revision),
        )
        if not cursor.rowcount:
            return False
        self.conn.execute(
            """
            UPDATE news_delivery_queue q
               SET state = 'pending', attempts = 0, lease_token = NULL, last_attempt_at_ms = NULL,
                   settled_at_ms = NULL, next_attempt_at_ms = %s, updated_at_ms = %s
             WHERE q.event_id = %s AND q.content_revision = %s AND q.kind = 'update' AND q.state = 'dead'
               AND NOT EXISTS (SELECT 1 FROM news_deliveries d WHERE d.intent_id = q.intent_id)
            """,
            (int(now_ms), int(now_ms), event_id, revision),
        )
        return True

    # ------------------------------------------------------------------ optional read budget
    def reserve_extra_read(self, *, lineage_id: str, target_ref: str, now_ms: int) -> bool:
        row = self.conn.execute(
            """
            UPDATE news_semantic_work
               SET extra_read_state = 'reserved', extra_read_target_ref = %s, updated_at_ms = %s
             WHERE lineage_id = %s AND extra_read_state IS NULL
            RETURNING event_id
            """,
            (target_ref, int(now_ms), lineage_id),
        ).fetchone()
        return row is not None

    def record_read_outcome(self, *, lineage_id: str, outcome: str, now_ms: int) -> bool:
        if outcome not in EXTRA_READ_OUTCOMES:
            raise ValueError("news_extra_read_outcome_invalid")
        cursor = self.conn.execute(
            "UPDATE news_semantic_work SET extra_read_state = %s, updated_at_ms = %s WHERE lineage_id = %s",
            (outcome, int(now_ms), lineage_id),
        )
        return bool(cursor.rowcount)

    def attach_extra_evidence(
        self,
        *,
        event_id: str,
        lineage_id: str,
        evidence_json: str,
        focus_claim_refs: Sequence[str],
        now_ms: int,
    ) -> int | None:
        """Want one more revision of the same lineage whose input is only the attached material."""

        row = self.conn.execute(
            """
            UPDATE news_semantic_work
               SET wanted_revision = wanted_revision + 1,
                   attached_evidence = %s::jsonb, focus_claim_refs = %s::jsonb,
                   attempts = 0, next_attempt_at_ms = %s, published_at_ms = NULL, updated_at_ms = %s
             WHERE event_id = %s AND lineage_id = %s
            RETURNING wanted_revision
            """,
            (evidence_json, _dumps(sorted(set(focus_claim_refs))), int(now_ms), int(now_ms), event_id, lineage_id),
        ).fetchone()
        return None if row is None else int(row["wanted_revision"])

    # ------------------------------------------------------------------ judgment cache and retention
    def judgment_cache_answers(self, cache_keys: Sequence[str]) -> dict[str, dict[str, Any]]:
        """One statement for a whole question set's cached answers."""

        rows = self.conn.execute(
            "SELECT cache_key, answer FROM news_judgment_cache WHERE cache_key = ANY(%s)", (list(cache_keys),)
        ).fetchall()
        return {str(row["cache_key"]): dict(row["answer"]) for row in rows}

    def put_judgment_cache_answers(self, *, answers: Mapping[str, str], now_ms: int) -> int:
        """One statement per answered batch; the first stored answer for a key wins."""

        cursor = self.conn.execute(
            """
            INSERT INTO news_judgment_cache (cache_key, answer, created_at_ms)
            SELECT cache_key, answer::jsonb, %s FROM unnest(%s::text[], %s::text[]) AS a(cache_key, answer)
            ON CONFLICT (cache_key) DO NOTHING
            """,
            (int(now_ms), list(answers), list(answers.values())),
        )
        return int(cursor.rowcount or 0)

    def purge_semantic_caches(self, *, now_ms: int, limit: int = PURGE_BATCH_MAX) -> int:
        """Janitor retention: judgment answers and stage checkpoints older than 14 days, bounded."""

        cutoff = int(now_ms) - JUDGMENT_CACHE_RETENTION_MS
        bounded = max(1, min(int(limit), PURGE_BATCH_MAX))
        answers = self.conn.execute(
            """
            DELETE FROM news_judgment_cache WHERE cache_key IN (
              SELECT cache_key FROM news_judgment_cache WHERE created_at_ms < %s
               ORDER BY created_at_ms, cache_key LIMIT %s
            )
            """,
            (cutoff, bounded),
        )
        checkpoints = self.conn.execute(
            """
            DELETE FROM news_semantic_checkpoints WHERE (work_id, stage) IN (
              SELECT work_id, stage FROM news_semantic_checkpoints WHERE created_at_ms < %s
               ORDER BY created_at_ms, work_id, stage LIMIT %s
            )
            """,
            (cutoff, bounded),
        )
        return int(answers.rowcount or 0) + int(checkpoints.rowcount or 0)


__all__ = [
    "INTENT_LEASE_MS",
    "EventUpdateConflict",
    "EventUpdateStorage",
    "IntentLeaseLost",
    "SemanticLease",
    "delivered_text",
    "frozen_input",
    "item_evidence",
    "read_target_item_id",
    "read_target_ref",
    "reader_revision",
    "revision_evidence",
    "select_receipts",
]
