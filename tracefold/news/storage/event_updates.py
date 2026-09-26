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

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal, cast

from ..evidence import query_for
from ..models import MarketAsset, market_type_of
from ..taxonomy import source_authority
from ..updates.contracts import EventUpdate, Evidence, FrozenInput, IdentityHint, PriorClaim, ReadTarget, Source
from ..updates.identity import digest, identity
from ..updates.notification import DeliveredText, FrozenCard, NotificationPlan
from .decisions import DecisionStorage
from .evidence import EvidenceStorage
from .sql_values import _dumps
from .trade_projection import TradeProjectionStorage

NEWS_CHANNEL: Final = "news"
SEMANTIC_ATTEMPTS_MAX: Final = 3
# Delay before the next attempt after the Nth failed one (1-based), per wanted revision.
SEMANTIC_RETRY_MS: Final = (15_000, 60_000, 300_000)
# A pending revision whose broker wake is older than this is woken again by the repair turn.
SEMANTIC_WAKE_STALE_MS: Final = 15_000
NOTIFICATION_ATTEMPTS_MAX: Final = 3
NOTIFICATION_RETRY_MS: Final = (30_000, 120_000, 600_000)
# The queue's own CHECK bounds an intent at three attempts.
INTENT_ATTEMPTS_MAX: Final = 3
INTENT_RETRY_MS: Final = (30_000, 120_000, 600_000)
# Longer than one notification stage (20 s): compose, freeze and send happen under one lease.
# Covers one notification stage plus card composition and the send.
INTENT_LEASE_MS: Final = 120_000
RECEIPT_RECALL_MAX: Final = 16
JUDGMENT_CACHE_RETENTION_MS: Final = 14 * 24 * 3_600_000
PURGE_BATCH_MAX: Final = 1_000
READER_REVISION_PREFIX: Final = "reader_v1"
EXTRA_READ_OUTCOMES: Final = frozenset({"attached", "no_material", "unavailable_or_budget_exhausted"})
# The public kind of a PublicUpdate in the News outbox. A catalyst delta keeps the existing kind.
PUBLIC_TRADE_KINDS: Final[dict[str, str]] = {"catalyst_delta": "catalyst", "source_update": "source_update"}
# Related Events whose adopted claims are compared with this Event's claims. Every current/prior pair
# is a relation question, so the recall is bounded by claims, not only by Events.
RELATED_PRIOR_EVENTS_MAX: Final = 8
RELATED_PRIOR_CLAIMS_MAX: Final = 8
# Code-prepared optional read targets: related Events' leader Items not already in the input.
READ_TARGETS_MAX: Final = 4
READ_TARGET_PREFIX: Final = "news_item:"
_WAKE_STATE_LIMIT: Final = 1_000
SEMANTIC_WAKE_STATE_SQL: Final = f"""
    WITH pending AS MATERIALIZED (
      SELECT attempts, updated_at_ms FROM news_semantic_work
       WHERE done_revision IS NULL OR done_revision < wanted_revision
       ORDER BY next_attempt_at_ms, event_id
       LIMIT {_WAKE_STATE_LIMIT}
    )
    SELECT count(*) FILTER (WHERE attempts < {SEMANTIC_ATTEMPTS_MAX}) AS pending,
           min(updated_at_ms) FILTER (WHERE attempts < {SEMANTIC_ATTEMPTS_MAX}) AS oldest_pending_at_ms,
           count(*) FILTER (WHERE attempts >= {SEMANTIC_ATTEMPTS_MAX}) AS expired
      FROM pending
"""  # noqa: S608 - code-owned integer constants only
# The semantic stage's 24 h health: completed turns, adoptions and visibly failed work. Model health reads
# these, not legacy verdicts; pending work is bounded like the wake state.
SEMANTIC_STATUS_SQL: Final = f"""
    SELECT
      (SELECT count(*) FROM news_semantic_observations WHERE completed_at_ms >= %(since)s)
        AS semantic_observations_24h,
      (SELECT count(*) FROM news_event_updates WHERE adopted_at_ms >= %(since)s) AS semantic_adopted_24h,
      (SELECT count(*) FROM news_semantic_work WHERE last_outcome = 'failed' AND updated_at_ms >= %(since)s)
        AS semantic_failed_24h,
      (SELECT count(*) FROM (
         SELECT 1 FROM news_semantic_work
          WHERE done_revision IS NULL OR done_revision < wanted_revision
          LIMIT {_WAKE_STATE_LIMIT}
       ) pending) AS semantic_pending
"""  # noqa: S608 - code-owned integer constant only
SEMANTIC_FAILED_CODES_SQL: Final = """
    SELECT COALESCE(last_error_code, 'unknown') AS code, count(*) AS n
      FROM news_semantic_work
     WHERE last_outcome = 'failed' AND updated_at_ms >= %s
     GROUP BY 1
"""
_ADOPT_LOCK_NAMESPACE: Final = 0x4E455755  # 'NEWU', distinct from the storyline lock namespace.

IntentOutcome = Literal["sent", "not_sent", "ambiguous"]


class EventUpdateConflict(ValueError):
    """A stored insert-only fact disagrees with the value offered for the same identity."""


class IntentLeaseLost(RuntimeError):
    """The caller no longer owns the intent lease it is writing under."""


@dataclass(frozen=True, slots=True)
class SemanticLease:
    event_id: str
    wanted_revision: int
    lineage_id: str
    lease_token: str
    attempts: int


def _retry_delay(delays: Sequence[int], attempts: int) -> int:
    return int(delays[max(0, min(int(attempts), len(delays)) - 1)])


def reader_revision(
    stamp_ms: int,
    ledger: Sequence[object],
    blocked_claim_refs: Iterable[str],
    watch_symbols: Iterable[str],
) -> str:
    """One reader version: the sent-ledger token read at `stamp_ms`, blocked claims and watchlist.

    The stamp is carried in the revision so the CAS re-reads the ledger at the same stamp: the token's
    window is open above it, so a receipt settled after the snapshot is exactly what changes it.
    """

    material = [list(ledger), sorted(set(blocked_claim_refs)), sorted(set(watch_symbols))]
    return f"{READER_REVISION_PREFIX}:{int(stamp_ms)}:{digest(material)}"


def reader_revision_stamp(revision: str) -> int:
    prefix, _, rest = str(revision).partition(":")
    stamp, _, value = rest.partition(":")
    if prefix != READER_REVISION_PREFIX or not stamp.isdigit() or len(value) != 64:
        raise ValueError("news_reader_revision_invalid")
    return int(stamp)


def _legacy_receipt_body(row: Mapping[str, Any]) -> str:
    card = row.get("card") or {}
    context = row.get("history_context") or {}
    header = card.get("header") if isinstance(card, Mapping) else None
    title = header.get("title") if isinstance(header, Mapping) else None
    headline = str(title.get("content") or "") if isinstance(title, Mapping) else ""
    if not headline.strip() and isinstance(context, Mapping):
        headline = str(context.get("headline_zh") or "")
    why = str(context.get("why_zh") or "") if isinstance(context, Mapping) else ""
    return "\n\n".join(part.strip() for part in (headline, why) if part.strip())


def delivered_text(row: Mapping[str, Any]) -> DeliveredText | None:
    """One actually sent receipt as the reader saw it.

    An update intent retains its exact body. A legacy `first` card retained only its rendered card and
    history context, so its text is decoded from the headline and why lines; its `legacy_intent:` id
    marks it as a decoded legacy receipt.
    """

    receipt = row.get("receipt") or {}
    message_id = None
    if isinstance(receipt, Mapping):
        value = receipt.get("provider_message_id", receipt.get("message_id"))
        message_id = None if value is None else str(value)
    if row["kind"] == "update":
        body = str(row["body"])
        payload_sha256 = str(row["payload_sha256"])
    else:
        body = _legacy_receipt_body(row)
        if not body:
            return None
        payload_sha256 = digest(body)
    return DeliveredText(
        intent_id=str(row["intent_id"]),
        channel=NEWS_CHANNEL,
        state="sent",
        body=body,
        payload_sha256=payload_sha256,
        received_at_ms=int(row["settled_at_ms"]),
        provider_message_id=message_id,
    )


def select_receipts(
    event_id: str,
    band_event_ids: Sequence[str],
    rows: Sequence[Mapping[str, Any]],
    *,
    limit: int = RECEIPT_RECALL_MAX,
) -> tuple[DeliveredText, ...]:
    """The Event's own sent intents first, newest first; then the newest receipt of each band Event.

    Band order is the existing reader-history order (targeted, title similarity, recent).
    """

    own: list[DeliveredText] = []
    newest: dict[str, DeliveredText] = {}
    for row in rows:  # already newest first
        receipt = delivered_text(row)
        if receipt is None:
            continue
        if row["event_id"] == event_id:
            own.append(receipt)
        else:
            newest.setdefault(str(row["event_id"]), receipt)
    band = [newest[value] for value in dict.fromkeys(band_event_ids) if value in newest]
    return tuple([*own, *band][:limit])


def _item_text(item: Mapping[str, Any]) -> str:
    text = str(item.get("evidence_text") or "").strip()
    if text:
        return text
    return "\n".join(
        part for part in (str(item.get("title") or "").strip(), str(item.get("description") or "").strip()) if part
    )


def item_evidence(item: Mapping[str, Any]) -> Evidence | None:
    """One stored provider Item as model-visible evidence with code-owned provenance.

    The source clocks are the Item's first observation and provider publication; the authority is the
    News source-authority classifier over the reporting origin and URL, never a model value.
    """

    text = _item_text(item)
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
            origin_id=origin,
            published_at_ms=None if item.get("published_at_ms") is None else int(item["published_at_ms"]),
            first_available_at_ms=int(item["observed_at_ms"]),
            url=url,
            source_authority=source_authority(tuple(value for value in (origin, url) if value)),
        ),
    )


def revision_evidence(item: Mapping[str, Any], revision: Mapping[str, Any]) -> Evidence | None:
    """A later body of one provider record: the same provenance, its own body identity and clock.

    The first body stays evidence too; a correction or an added exemption is visible only beside the
    text it revised.
    """

    text = str(revision.get("evidence_text") or "").strip()
    first = item_evidence(item)
    if not text or first is None:
        return None
    return Evidence.issue(
        text,
        first.source.model_copy(
            update={
                "artifact_revision": str(revision["body_sha256"]),
                "first_available_at_ms": int(revision["received_at_ms"]),
            }
        ),
    )


def read_target_ref(item_id: str) -> str:
    return f"{READ_TARGET_PREFIX}{item_id}"


def read_target_item_id(ref: str) -> str | None:
    value = ref.removeprefix(READ_TARGET_PREFIX)
    return value if ref.startswith(READ_TARGET_PREFIX) and value else None


def _identity_hints(evidence: Sequence[Evidence], symbols: Iterable[str]) -> tuple[IdentityHint, ...]:
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
            if surface in item.text
        )
    return tuple(hints)


def _related_prior(documents: Sequence[Mapping[str, Any]], own: set[str]) -> tuple[PriorClaim, ...]:
    """At most RELATED_PRIOR_CLAIMS_MAX current claims of related Events, in retrieval order."""

    prior: list[PriorClaim] = []
    for document in documents:
        head = EventUpdate.model_validate(document)
        retired = set(head.retired_claim_refs)
        for claim in head.claims:
            if len(prior) >= RELATED_PRIOR_CLAIMS_MAX:
                return tuple(prior)
            if claim.ref in retired or claim.ref in own:
                continue
            own.add(claim.ref)
            prior.append(PriorClaim(event_id=head.event_id, content_revision=head.content_revision, claim=claim))
    return tuple(prior)


def frozen_input(event_id: str, material: Mapping[str, Any]) -> FrozenInput:
    """Assemble the frozen semantic input from one consistent read.

    Evidence is only material absent from the adopted head: newly joined members, later bodies of
    an existing Item, or a bounded optional read. The complete snapshot is still read consistently
    to identify that delta. Prior claims are this Event's adopted head claims plus a bounded set of
    related Events' head claims recalled by existing candidate retrieval. Assembly carries unaffected
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
                revisions.get(str(item_id), ()), key=lambda row: (int(row["received_at_ms"]), row["body_sha256"])
            ):
                revised = revision_evidence(item, revision)
                if revised is not None:
                    evidence.append(revised)
        focus = ()
    if not evidence:
        raise LookupError("news_event_input_missing")
    unique = tuple({row.ref: row for row in evidence}.values())
    if head_document is not None:
        # The head's evidence refs are the material already analyzed and adopted. Ref identity
        # includes the publisher, artifact, body revision, attribution and text, so a genuine
        # source/body correction remains new even when it belongs to the same Item.
        adopted_refs = {row.ref for row in EventUpdate.model_validate(head_document).evidence}
        adopted_refs.update(str(ref) for ref in (work or {}).get("processed_evidence_refs") or ())
        unique = tuple(row for row in unique if row.ref not in adopted_refs)
    elif work is not None:
        processed_refs = set(work.get("processed_evidence_refs") or ())
        unique = tuple(row for row in unique if row.ref not in processed_refs)
    prior: tuple[PriorClaim, ...] = ()
    if head_document is not None:
        head = EventUpdate.model_validate(head_document)
        prior = tuple(
            PriorClaim(event_id=head.event_id, content_revision=head.content_revision, claim=claim)
            for claim in head.claims
        )
    read_targets: tuple[ReadTarget, ...] = ()
    hints: tuple[IdentityHint, ...] = ()
    if not attached:
        # An optional read's revision re-asks only its focus claims against the attached material.
        prior = (*prior, *_related_prior(material.get("related_heads") or (), {row.claim.ref for row in prior}))
        read_targets = tuple(
            ReadTarget(
                ref=read_target_ref(str(row["item_id"])), action="load_prior_statement", description=str(row["title"])
            )
            for row in material.get("read_targets") or ()
            if str(row.get("title") or "").strip()
        )
        hints = _identity_hints(unique, material.get("grounded_assets") or ())
    wanted = int(work["wanted_revision"]) if work is not None else max(1, int(material.get("evidence_version") or 1))
    lineage = str(work["lineage_id"]) if work is not None else identity("lineage", event_id, wanted)
    return FrozenInput(
        event_id=event_id,
        revision=wanted,
        lineage_id=lineage,
        evidence=unique,
        prior=prior,
        read_targets=read_targets,
        focus_claim_refs=focus,
        identity_hints=hints,
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
        """Lease due pending work, spending one attempt of its wanted revision."""

        row = self.conn.execute(
            """
            UPDATE news_semantic_work
               SET attempts = attempts + 1, lease_token = %s, leased_until_ms = %s, updated_at_ms = %s
             WHERE event_id = %s
               AND (done_revision IS NULL OR done_revision < wanted_revision)
               AND attempts < %s
               AND next_attempt_at_ms <= %s
               AND (leased_until_ms IS NULL OR leased_until_ms <= %s)
            RETURNING event_id, wanted_revision, lineage_id, lease_token, attempts
            """,
            (
                lease_token,
                int(now_ms) + int(lease_ms),
                int(now_ms),
                event_id,
                SEMANTIC_ATTEMPTS_MAX,
                int(now_ms),
                int(now_ms),
            ),
        ).fetchone()
        if row is None:
            return None
        return SemanticLease(
            event_id=str(row["event_id"]),
            wanted_revision=int(row["wanted_revision"]),
            lineage_id=str(row["lineage_id"]),
            lease_token=str(row["lease_token"]),
            attempts=int(row["attempts"]),
        )

    def defer_semantic_event(
        self, *, event_id: str, lease_token: str | None, reason: str, now_ms: int, retry_after_ms: int = 0
    ) -> bool:
        """Release a lease for a retry; the third attempt of a revision leaves it visibly `failed`."""

        cursor = self.conn.execute(
            """
            UPDATE news_semantic_work
               SET lease_token = NULL, leased_until_ms = NULL,
                   last_outcome = CASE WHEN attempts >= %s THEN 'failed' ELSE %s END,
                   last_error_code = %s,
                   next_attempt_at_ms = %s::bigint
                     + GREATEST(%s::bigint, (%s::bigint[])[GREATEST(1, LEAST(attempts, %s))]),
                   updated_at_ms = %s
             WHERE event_id = %s AND (%s::text IS NULL OR lease_token = %s)
            """,
            (
                SEMANTIC_ATTEMPTS_MAX,
                reason,
                reason,
                int(now_ms),
                int(retry_after_ms),
                list(SEMANTIC_RETRY_MS),
                len(SEMANTIC_RETRY_MS),
                int(now_ms),
                event_id,
                lease_token,
                lease_token,
            ),
        )
        return bool(cursor.rowcount)

    def fail_semantic_event(self, *, event_id: str, lease_token: str | None, error_code: str, now_ms: int) -> bool:
        """A contract fault: visible as `failed` with its code, not retried until a new revision."""

        cursor = self.conn.execute(
            """
            UPDATE news_semantic_work
               SET lease_token = NULL, leased_until_ms = NULL, attempts = %s,
                   last_outcome = 'failed', last_error_code = %s, updated_at_ms = %s
             WHERE event_id = %s AND (%s::text IS NULL OR lease_token = %s)
            """,
            (SEMANTIC_ATTEMPTS_MAX, error_code, int(now_ms), event_id, lease_token, lease_token),
        )
        return bool(cursor.rowcount)

    def finish_semantic_work(self, *, work_id: str, reason: str, now_ms: int) -> bool:
        """Mark the observed revision done and remember its analyzed evidence atomically."""

        observed = self._observed_work(work_id)
        cursor = self.conn.execute(
            """
            UPDATE news_semantic_work
               SET done_revision = LEAST(wanted_revision, GREATEST(COALESCE(done_revision, 0), %s)),
                   processed_evidence_refs = ARRAY(
                       SELECT DISTINCT ref FROM unnest(processed_evidence_refs || %s::text[]) AS ref
                   ),
                   attempts = CASE WHEN %s >= wanted_revision THEN 0 ELSE attempts END,
                   lease_token = NULL, leased_until_ms = NULL,
                   last_outcome = %s, last_error_code = NULL,
                   next_attempt_at_ms = %s, updated_at_ms = %s
             WHERE event_id = %s
            """,
            (
                observed["input_revision"],
                list(observed["evidence_refs"]),
                observed["input_revision"],
                reason,
                int(now_ms),
                int(now_ms),
                observed["event_id"],
            ),
        )
        return bool(cursor.rowcount)

    def defer_semantic_work(self, *, work_id: str, reason: str, now_ms: int) -> bool:
        observed = self._observed_work(work_id)
        return self.defer_semantic_event(
            event_id=str(observed["event_id"]), lease_token=None, reason=reason, now_ms=now_ms
        )

    def _observed_work(self, work_id: str) -> Mapping[str, Any]:
        # The port addresses work by its code-owned identity; its Event and input revision are the ones
        # the observation of that work recorded. Every service path saves one before finishing.
        rows = self.conn.execute(
            """
            SELECT event_id, input_revision, evidence_refs
              FROM news_semantic_observations WHERE work_id = %s
            """,
            (work_id,),
        ).fetchall()
        if not rows or len({str(row["event_id"]) for row in rows}) != 1:
            raise LookupError("news_semantic_work_unknown")
        return {
            "event_id": str(rows[0]["event_id"]),
            "input_revision": max(int(row["input_revision"]) for row in rows),
            "evidence_refs": tuple({ref for row in rows for ref in row["evidence_refs"]}),
        }

    def pending_semantic_event_ids(self, *, now_ms: int, limit: int) -> list[str]:
        rows = self.conn.execute(
            """
            SELECT event_id FROM news_semantic_work
             WHERE (done_revision IS NULL OR done_revision < wanted_revision)
               AND attempts < %s
               AND next_attempt_at_ms <= %s
               AND (leased_until_ms IS NULL OR leased_until_ms <= %s)
               AND (published_at_ms IS NULL OR published_at_ms <= %s)
             ORDER BY next_attempt_at_ms, event_id
             LIMIT %s
            """,
            (SEMANTIC_ATTEMPTS_MAX, int(now_ms), int(now_ms), int(now_ms) - SEMANTIC_WAKE_STALE_MS, int(limit)),
        ).fetchall()
        return [str(row["event_id"]) for row in rows]

    def semantic_work(self, event_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM news_semantic_work WHERE event_id = %s", (event_id,)).fetchone()
        return None if row is None else dict(row)

    # ------------------------------------------------------------------ semantic input and results
    def semantic_input_material(self, event_id: str, *, now_ms: int) -> dict[str, Any]:
        work = self.conn.execute(
            """
            SELECT wanted_revision, lineage_id, attached_evidence, focus_claim_refs, processed_evidence_refs
              FROM news_semantic_work WHERE event_id = %s
            """,
            (event_id,),
        ).fetchone()
        snapshot = self.conn.execute(
            """
            SELECT evidence_version, snapshot FROM news_event_evidence_snapshots
             WHERE event_id = %s AND provenance = 'observed'
             ORDER BY evidence_version DESC LIMIT 1
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
            (str(member["item_id"]), str(body_sha))
            for member in members
            for body_sha in member.get("body_revisions") or ()
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
                SELECT r.item_id, r.body_sha256, r.evidence_text, r.received_at_ms
                  FROM news_item_revisions r
                  JOIN unnest(%s::text[], %s::text[]) AS frozen(item_id, body_sha256)
                    ON frozen.item_id = r.item_id AND frozen.body_sha256 = r.body_sha256
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
            "items": [dict(row) for row in items],
            "revisions": [dict(row) for row in revisions],
            "head": self.event_update_head_document(event_id),
            "related_heads": self._related_head_documents(related_ids),
            "read_targets": self._read_target_rows(related_ids, exclude_item_ids=item_ids),
            "grounded_assets": [str(value) for value in card.get("grounded_assets") or ()],
        }

    def _related_event_ids(
        self, event_id: str, card: Mapping[str, Any], leader: Mapping[str, Any], *, now_ms: int
    ) -> list[str]:
        """Related Events by the existing bounded candidate retrieval, in its priority order."""

        assets = tuple(
            MarketAsset(str(symbol), market_type_of(card.get("asset_class")))
            for symbol in card.get("grounded_assets") or ()
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
        row = self.conn.execute(SEMANTIC_STATUS_SQL, {"since": since}).fetchone()
        codes = self.conn.execute(SEMANTIC_FAILED_CODES_SQL, (since,)).fetchall()
        values = {key: int(value or 0) for key, value in dict(row or {}).items()}
        return {
            "semantic_observations_24h": values.get("semantic_observations_24h", 0),
            "semantic_adopted_24h": values.get("semantic_adopted_24h", 0),
            "semantic_failed_24h": values.get("semantic_failed_24h", 0),
            "semantic_pending": values.get("semantic_pending", 0),
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
        evidence_refs: Sequence[str],
    ) -> dict[str, Any]:
        """Insert-only by result id; the stored row, with its original completion clock, is returned."""

        self.conn.execute(
            """
            INSERT INTO news_semantic_observations (
              result_id, work_id, event_id, input_revision, input_sha256, program_identity,
              completed_at_ms, understanding, evidence_refs
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
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
                list(evidence_refs),
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
        self.conn.execute("SET LOCAL lock_timeout = '2500ms'")
        self.conn.execute("SELECT pg_advisory_xact_lock(%s, hashtext(%s))", (_ADOPT_LOCK_NAMESPACE, event_id))
        head = self.conn.execute(
            "SELECT update_ref, input_revision FROM news_event_update_heads WHERE event_id = %s", (event_id,)
        ).fetchone()
        if (None if head is None else str(head["update_ref"])) != expected_head_ref:
            return False
        if head is not None and update.input_revision < int(head["input_revision"]):
            raise EventUpdateConflict("news_update_input_revision_downgrade")
        inserted = self.conn.execute(
            """
            INSERT INTO news_event_updates (
              event_id, content_revision, input_revision, previous_content_revision, adopted_at_ms,
              observation_result_id, document
            ) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (event_id, content_revision) DO NOTHING
            RETURNING content_revision
            """,
            (
                event_id,
                update.content_revision,
                update.input_revision,
                update.previous_content_revision,
                update.adopted_at_ms,
                observation_result_id,
                document_json,
            ),
        ).fetchone()
        if inserted is None:
            # A content state this Event already adopted once. Its stored revision is the fact; a
            # different document for the same identity is not silently re-pointed.
            raise EventUpdateConflict("news_event_update_revision_exists")
        self.conn.execute(
            """
            INSERT INTO news_event_update_heads (event_id, content_revision, input_revision, update_ref, adopted_at_ms)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (event_id) DO UPDATE SET
              content_revision = EXCLUDED.content_revision,
              input_revision = GREATEST(news_event_update_heads.input_revision, EXCLUDED.input_revision),
              update_ref = EXCLUDED.update_ref,
              adopted_at_ms = EXCLUDED.adopted_at_ms
            """,
            (event_id, update.content_revision, update.input_revision, update.ref, update.adopted_at_ms),
        )
        outbox = cast(TradeProjectionStorage, self)
        for kind, payload in public_rows:
            # The outbox clock is the semantic completion the public payload carries, never adoption:
            # a slower adoption must not make the same public fact look newer to Trading.
            if not outbox.enqueue_trade_event(
                kind=kind,
                source_fact_key=event_id,
                source_revision=update.content_revision,
                payload=payload,
                source_recorded_at_ms=int(cast(int, payload["semantic_completed_at_ms"])),
            ):
                raise EventUpdateConflict("news_public_update_conflict")
        self.conn.execute(
            """
            INSERT INTO news_notification_work (
              event_id, channel, content_revision, state, attempts, next_attempt_at_ms, updated_at_ms
            ) VALUES (%s, %s, %s, 'pending', 0, %s, %s)
            ON CONFLICT (event_id, channel) DO UPDATE SET
              content_revision = EXCLUDED.content_revision,
              state = 'pending',
              attempts = 0,
              next_attempt_at_ms = EXCLUDED.next_attempt_at_ms,
              updated_at_ms = EXCLUDED.updated_at_ms
            """,
            (event_id, NEWS_CHANNEL, update.content_revision, int(now_ms), int(now_ms)),
        )
        return True

    # ------------------------------------------------------------------ notification snapshot and plan
    def _blocked_claim_refs(self, event_id: str) -> list[str]:
        rows = self.conn.execute(
            """
            SELECT claim_refs FROM news_deliveries
             WHERE event_id = %s AND kind = 'update' AND state IN ('sending', 'ambiguous')
            """,
            (event_id,),
        ).fetchall()
        return sorted({str(ref) for row in rows for ref in row["claim_refs"] or ()})

    def _reader_revision(self, *, event_id: str, stamp_ms: int, watch_symbols: Iterable[str]) -> str:
        ledger = cast(DecisionStorage, self).reader_history_revision(now_ms=stamp_ms)
        return reader_revision(stamp_ms, ledger, self._blocked_claim_refs(event_id), watch_symbols)

    def notification_snapshot_material(
        self, *, event_id: str, channel: str, now_ms: int, watch_symbols: Iterable[str]
    ) -> dict[str, Any] | None:
        """The pending head and the actual-reader receipts recalled by the reader-history bands."""

        work = self.conn.execute(
            "SELECT content_revision, state FROM news_notification_work WHERE event_id = %s AND channel = %s",
            (event_id, channel),
        ).fetchone()
        if work is None or work["state"] != "pending":
            return None
        head = self.event_update_head_document(event_id)
        if head is None or head.get("content_revision") != work["content_revision"]:
            return None
        blocked = self._blocked_claim_refs(event_id)
        ledger = cast(DecisionStorage, self).reader_history_revision(now_ms=now_ms)
        history = cast(DecisionStorage, self).reader_history(event_id=event_id, now_ms=now_ms)
        band = [row.event_id for row in history.told_source_rows]
        rows = self.conn.execute(
            """
            SELECT intent_id, event_id, kind, body, payload_sha256, settled_at_ms, receipt, card, history_context
              FROM news_deliveries
             WHERE event_id = ANY(%s) AND kind IN ('first', 'update') AND state = 'sent'
               AND delete_state IS DISTINCT FROM 'deleted'
               AND settled_at_ms < %s
             ORDER BY settled_at_ms DESC, intent_id
            """,
            ([event_id, *band], int(now_ms)),
        ).fetchall()
        return {
            "head": head,
            "blocked": blocked,
            "revision": reader_revision(now_ms, ledger, blocked, watch_symbols),
            "band_event_ids": band,
            "receipt_rows": [dict(row) for row in rows],
        }

    def record_notification_plan(
        self,
        *,
        plan: NotificationPlan,
        plan_json: str,
        lease_token: str,
        watch_symbols: Iterable[str],
        now_ms: int,
        lease_ms: int = INTENT_LEASE_MS,
    ) -> dict[str, Any] | None:
        """CAS head and reader revision, persist the plan, and reserve its one stable intent.

        Returns the leased intent (`intent_id`, stored `frozen_card`) or None. None leaves an owned,
        sending, sent or ambiguous identity exactly as it is.
        """

        head = self.conn.execute(
            "SELECT event_id, content_revision FROM news_event_update_heads WHERE update_ref = %s",
            (plan.update_ref,),
        ).fetchone()
        if head is None:
            return None
        event_id = str(head["event_id"])
        work = self.conn.execute(
            """
            SELECT state, content_revision, attempts FROM news_notification_work
             WHERE event_id = %s AND channel = %s FOR UPDATE
            """,
            (event_id, plan.channel),
        ).fetchone()
        if work is None or work["state"] != "pending" or work["content_revision"] != head["content_revision"]:
            return None
        stamp = reader_revision_stamp(plan.reader_revision)
        if (
            self._reader_revision(event_id=event_id, stamp_ms=stamp, watch_symbols=watch_symbols)
            != plan.reader_revision
        ):
            return None
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
        if any(row["lease_token"] is not None and int(row["next_attempt_at_ms"]) > int(now_ms) for row in others):
            return None
        if others:
            # Superseded unsent reservations of this Event: retire them, never a frozen send.
            self.conn.execute(
                "DELETE FROM news_delivery_queue WHERE intent_id = ANY(%s)",
                ([str(row["intent_id"]) for row in others],),
            )
        attempts = int(work["attempts"])
        if plan.action == "no_notification":
            self._settle_work(event_id, plan, plan_json, state="done", attempts=0, next_at_ms=now_ms, now_ms=now_ms)
            return None
        if plan.action == "unresolved":
            self._retry_work(event_id, plan, plan_json, attempts=attempts, now_ms=now_ms)
            return None
        intent_id = plan.intent_id
        ledger = self.conn.execute("SELECT state FROM news_deliveries WHERE intent_id = %s", (intent_id,)).fetchone()
        if ledger is not None:
            if ledger["state"] in ("sent", "terminal"):
                # This exact selection already reached its final outcome: the plan is recorded, not resent.
                self._complete_plan(event_id, plan, plan_json, attempts=attempts, now_ms=now_ms)
            else:
                self._retry_work(event_id, plan, plan_json, attempts=attempts, now_ms=now_ms)
            return None
        selected = list(plan.selected_claim_refs)
        reserved = self.conn.execute(
            """
            INSERT INTO news_delivery_queue (
              intent_id, event_id, kind, state, attempts, enqueued_at_ms, next_attempt_at_ms,
              last_attempt_at_ms, updated_at_ms, content_revision, claim_refs, plan_key, lease_token
            ) VALUES (%s, %s, 'update', 'pending', 1, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
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
            ),
        ).fetchone()
        frozen_card = None
        if reserved is None:
            existing = self.conn.execute(
                """
                SELECT state, attempts, lease_token, next_attempt_at_ms, frozen_card
                  FROM news_delivery_queue WHERE intent_id = %s FOR UPDATE
                """,
                (intent_id,),
            ).fetchone()
            if existing["state"] == "dead":
                self._complete_plan(event_id, plan, plan_json, attempts=attempts, now_ms=now_ms)
                return None
            if existing["lease_token"] is not None and int(existing["next_attempt_at_ms"]) > int(now_ms):
                return None
            if int(existing["attempts"]) >= INTENT_ATTEMPTS_MAX:
                self.conn.execute(
                    """
                    UPDATE news_delivery_queue
                       SET state = 'dead', lease_token = NULL, settled_at_ms = %s, updated_at_ms = %s,
                           error_code = COALESCE(error_code, 'news_delivery_attempts_exhausted')
                     WHERE intent_id = %s
                    """,
                    (int(now_ms), int(now_ms), intent_id),
                )
                self._complete_plan(event_id, plan, plan_json, attempts=attempts, now_ms=now_ms)
                return None
            self.conn.execute(
                """
                UPDATE news_delivery_queue
                   SET attempts = attempts + 1, lease_token = %s, next_attempt_at_ms = %s,
                       last_attempt_at_ms = %s, updated_at_ms = %s
                 WHERE intent_id = %s
                """,
                (lease_token, int(now_ms) + int(lease_ms), int(now_ms), int(now_ms), intent_id),
            )
            frozen_card = existing["frozen_card"]
        # The marker stays pending while the reserved intent is in flight. If this turn dies before
        # the send is settled, the repair turn re-plans after the lease and reclaims the same identity.
        spent = min(attempts + 1, NOTIFICATION_ATTEMPTS_MAX) if plan.deferred_claim_refs else attempts
        retry_ms = _retry_delay(NOTIFICATION_RETRY_MS, spent) if plan.deferred_claim_refs else 0
        self._settle_work(
            event_id,
            plan,
            plan_json,
            state="pending",
            attempts=spent,
            next_at_ms=int(now_ms) + max(int(lease_ms), retry_ms),
            now_ms=now_ms,
        )
        return {"intent_id": intent_id, "event_id": event_id, "frozen_card": frozen_card}

    def _complete_plan(
        self, event_id: str, plan: NotificationPlan, plan_json: str, *, attempts: int, now_ms: int
    ) -> None:
        # Deferred claims keep the marker pending for a later turn; otherwise this head is planned.
        if plan.deferred_claim_refs:
            self._retry_work(event_id, plan, plan_json, attempts=attempts, now_ms=now_ms)
        else:
            self._settle_work(event_id, plan, plan_json, state="done", attempts=0, next_at_ms=now_ms, now_ms=now_ms)

    def _complete_intent(self, event_id: str, content_revision: str, *, now_ms: int) -> None:
        """An intent reached its final outcome: its plan is complete unless the head moved on."""

        work = self.conn.execute(
            """
            SELECT attempts, plan, content_revision, state FROM news_notification_work
             WHERE event_id = %s AND channel = %s FOR UPDATE
            """,
            (event_id, NEWS_CHANNEL),
        ).fetchone()
        if work is None or work["plan"] is None or work["content_revision"] != content_revision:
            return
        plan = NotificationPlan.model_validate(work["plan"])
        self._complete_plan(event_id, plan, _dumps(work["plan"]), attempts=int(work["attempts"]), now_ms=now_ms)

    def _retry_work(self, event_id: str, plan: NotificationPlan, plan_json: str, *, attempts: int, now_ms: int) -> None:
        spent = min(attempts + 1, NOTIFICATION_ATTEMPTS_MAX)
        self._settle_work(
            event_id,
            plan,
            plan_json,
            state="pending",
            attempts=spent,
            next_at_ms=int(now_ms) + _retry_delay(NOTIFICATION_RETRY_MS, spent),
            now_ms=now_ms,
        )

    def _settle_work(
        self,
        event_id: str,
        plan: NotificationPlan,
        plan_json: str,
        *,
        state: str,
        attempts: int,
        next_at_ms: int,
        now_ms: int,
    ) -> None:
        self.conn.execute(
            """
            UPDATE news_notification_work
               SET state = %s, plan = %s::jsonb, reader_revision = %s, attempts = %s,
                   next_attempt_at_ms = %s, updated_at_ms = %s
             WHERE event_id = %s AND channel = %s
            """,
            (state, plan_json, plan.reader_revision, attempts, int(next_at_ms), int(now_ms), event_id, plan.channel),
        )

    def _pend_notification(self, event_id: str, *, next_at_ms: int, now_ms: int) -> None:
        self.conn.execute(
            """
            UPDATE news_notification_work SET state = 'pending', next_attempt_at_ms = %s, updated_at_ms = %s
             WHERE event_id = %s AND channel = %s
            """,
            (int(next_at_ms), int(now_ms), event_id, NEWS_CHANNEL),
        )

    # ------------------------------------------------------------------ intent card, send and settlement
    def save_intent_card(self, *, intent_id: str, lease_token: str, card_json: str, now_ms: int) -> dict[str, Any]:
        """Fenced insert-only frozen payload: an existing frozen card wins and is returned."""

        self.conn.execute(
            """
            UPDATE news_delivery_queue SET frozen_card = %s::jsonb, updated_at_ms = %s
             WHERE intent_id = %s AND kind = 'update' AND state = 'pending'
               AND lease_token = %s AND frozen_card IS NULL
            """,
            (card_json, int(now_ms), intent_id, lease_token),
        )
        row = self.conn.execute(
            "SELECT lease_token, state, frozen_card FROM news_delivery_queue WHERE intent_id = %s", (intent_id,)
        ).fetchone()
        if row is None or row["state"] != "pending" or row["lease_token"] != lease_token or row["frozen_card"] is None:
            raise IntentLeaseLost("news_intent_lease_lost")
        return dict(row["frozen_card"])

    def begin_intent_send(
        self,
        *,
        intent_id: str,
        lease_token: str,
        plan: NotificationPlan,
        card: FrozenCard,
        watch_symbols: Iterable[str],
        now_ms: int,
    ) -> bool:
        """Recheck head, reader revision, lease and in-flight overlap, then freeze `sending`.

        A changed head, reader or overlapping send releases the unsent reservation (its frozen card is
        kept for the same identity) and leaves notification pending. An existing ledger row is never
        touched.
        """

        queued = self.conn.execute(
            """
            SELECT event_id, state, lease_token, frozen_card, content_revision, claim_refs, plan_key
              FROM news_delivery_queue WHERE intent_id = %s AND kind = 'update' FOR UPDATE
            """,
            (intent_id,),
        ).fetchone()
        if queued is None or queued["state"] != "pending" or queued["lease_token"] != lease_token:
            return False
        frozen = queued["frozen_card"]
        if frozen is None or FrozenCard.model_validate(frozen) != card:
            raise EventUpdateConflict("news_intent_card_not_frozen")
        if self.conn.execute("SELECT 1 FROM news_deliveries WHERE intent_id = %s", (intent_id,)).fetchone():
            return False
        event_id = str(queued["event_id"])
        head = self.conn.execute(
            "SELECT update_ref FROM news_event_update_heads WHERE event_id = %s", (event_id,)
        ).fetchone()
        stamp = reader_revision_stamp(plan.reader_revision)
        changed = (
            head is None
            or head["update_ref"] != plan.update_ref
            or self._reader_revision(event_id=event_id, stamp_ms=stamp, watch_symbols=watch_symbols)
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
        if changed or overlap is not None:
            self.conn.execute(
                """
                UPDATE news_delivery_queue SET lease_token = NULL, next_attempt_at_ms = %s, updated_at_ms = %s
                 WHERE intent_id = %s
                """,
                (int(now_ms), int(now_ms), intent_id),
            )
            self._pend_notification(event_id, next_at_ms=now_ms, now_ms=now_ms)
            return False
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
              content_revision, claim_refs, body, payload_sha256, plan_key, history_context
            )
            SELECT %(intent)s, e.event_id, 'update', 'sending', %(card)s::jsonb, %(now)s, %(now)s,
                   %(revision)s, %(claim_refs)s::jsonb, %(body)s, %(sha)s, %(key)s,
                   jsonb_build_object(
                     'event_id', e.event_id,
                     'intent_id', %(intent)s::text,
                     'headline_zh', %(headline)s::text,
                     'why_zh', '',
                     'comparison_title', e.comparison_title,
                     'comparison_fingerprint', e.comparison_fingerprint,
                     'dedupe_family', e.dedupe_family,
                     'storyline_key', e.storyline_key,
                     'canonical_assets', canonical.symbols)
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
                "headline": card.headline_zh,
            },
        ).fetchone()
        return inserted is not None

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
    ) -> str | None:
        """Record the actual outcome of one frozen send; returns the ledger state written, if any.

        Only a `sending` row with this exact payload is settled. Sent keeps the body, digest, provider
        message id and the provider's own receipt (what an in-place edit is later fenced by); a provider
        that answers with no message id is recorded with none. Ambiguous is held for reconciliation; a
        retryable not-sent releases the identity for the same payload under the queue's attempt bound
        and the provider's own `Retry-After`, otherwise it is terminal.
        """

        ledger = self.conn.execute(
            """
            SELECT event_id, state, payload_sha256, content_revision FROM news_deliveries
             WHERE intent_id = %s FOR UPDATE
            """,
            (intent_id,),
        ).fetchone()
        if ledger is None or ledger["state"] != "sending" or ledger["payload_sha256"] != payload_sha256:
            return None
        event_id = str(ledger["event_id"])
        content_revision = str(ledger["content_revision"])
        queued = self.conn.execute(
            "SELECT attempts, lease_token FROM news_delivery_queue WHERE intent_id = %s FOR UPDATE", (intent_id,)
        ).fetchone()
        now_ms = int(settled_at_ms)
        if state == "sent":
            receipt = {
                "channel": NEWS_CHANNEL,
                "payload_sha256": payload_sha256,
                "provider_message_id": provider_message_id,
                "pushed_at_ms": now_ms,
                # The provider's own fields win: a Telegram receipt's push stamp and target identity are
                # what its enrichment edit is fenced by.
                **dict(provider_receipt or {}),
            }
            self.conn.execute(
                """
                UPDATE news_deliveries SET state = 'sent', receipt = %s::jsonb, error_code = NULL, settled_at_ms = %s
                 WHERE intent_id = %s
                """,
                (_dumps(receipt), now_ms, intent_id),
            )
            self.conn.execute("DELETE FROM news_delivery_queue WHERE intent_id = %s", (intent_id,))
            self._complete_intent(event_id, content_revision, now_ms=now_ms)
            return "sent"
        if state == "ambiguous":
            self.conn.execute(
                """
                UPDATE news_deliveries SET state = 'ambiguous', error_code = %s, settled_at_ms = %s
                 WHERE intent_id = %s
                """,
                (error_code or "send_outcome_ambiguous", now_ms, intent_id),
            )
            self.conn.execute("DELETE FROM news_delivery_queue WHERE intent_id = %s", (intent_id,))
            self._complete_intent(event_id, content_revision, now_ms=now_ms)
            return "ambiguous"
        owned = queued is not None and queued["lease_token"] == lease_token
        if retryable and owned and int(queued["attempts"]) < INTENT_ATTEMPTS_MAX:
            next_at_ms = now_ms + max(_retry_delay(INTENT_RETRY_MS, int(queued["attempts"])), int(retry_after_ms or 0))
            self.conn.execute("DELETE FROM news_deliveries WHERE intent_id = %s AND state = 'sending'", (intent_id,))
            self.conn.execute(
                """
                UPDATE news_delivery_queue
                   SET lease_token = NULL, error_code = %s, next_attempt_at_ms = %s, updated_at_ms = %s
                 WHERE intent_id = %s
                """,
                (error_code or "send_not_sent", next_at_ms, now_ms, intent_id),
            )
            self._pend_notification(event_id, next_at_ms=next_at_ms, now_ms=now_ms)
            return "not_sent"
        self.conn.execute(
            """
            UPDATE news_deliveries SET state = 'terminal', error_code = %s, settled_at_ms = %s
             WHERE intent_id = %s
            """,
            (error_code or "send_not_sent", now_ms, intent_id),
        )
        self.conn.execute(
            """
            UPDATE news_delivery_queue
               SET state = 'dead', lease_token = NULL, error_code = %s, settled_at_ms = %s, updated_at_ms = %s
             WHERE intent_id = %s
            """,
            (error_code or "send_not_sent", now_ms, now_ms, intent_id),
        )
        self._complete_intent(event_id, content_revision, now_ms=now_ms)
        return "terminal"

    def record_intent_card_failure(self, *, intent_id: str, lease_token: str, error_code: str, now_ms: int) -> bool:
        row = self.conn.execute(
            """
            UPDATE news_delivery_queue
               SET lease_token = NULL, error_code = %s,
                   state = CASE WHEN attempts >= %s THEN 'dead' ELSE 'pending' END,
                   settled_at_ms = CASE WHEN attempts >= %s THEN %s::bigint END,
                   next_attempt_at_ms = %s::bigint + (%s::bigint[])[GREATEST(1, LEAST(attempts, %s))],
                   updated_at_ms = %s
             WHERE intent_id = %s AND kind = 'update' AND state = 'pending' AND lease_token = %s
            RETURNING event_id, state, next_attempt_at_ms, content_revision
            """,
            (
                error_code,
                INTENT_ATTEMPTS_MAX,
                INTENT_ATTEMPTS_MAX,
                int(now_ms),
                int(now_ms),
                list(INTENT_RETRY_MS),
                len(INTENT_RETRY_MS),
                int(now_ms),
                intent_id,
                lease_token,
            ),
        ).fetchone()
        if row is None:
            return False
        if row["state"] == "pending":
            self._pend_notification(str(row["event_id"]), next_at_ms=int(row["next_attempt_at_ms"]), now_ms=now_ms)
        else:
            self._complete_intent(str(row["event_id"]), str(row["content_revision"]), now_ms=now_ms)
        return True

    def defer_notification_work(self, *, event_id: str, channel: str, now_ms: int) -> bool:
        """A planning turn failed before recording a plan: spend one attempt of the pending marker.

        The marker stays pending and visible; after the last attempt it is no longer due until a new
        adopted head resets it. Nothing else is touched.
        """

        row = self.conn.execute(
            """
            SELECT attempts FROM news_notification_work
             WHERE event_id = %s AND channel = %s AND state = 'pending' FOR UPDATE
            """,
            (event_id, channel),
        ).fetchone()
        if row is None:
            return False
        spent = min(int(row["attempts"]) + 1, NOTIFICATION_ATTEMPTS_MAX)
        self.conn.execute(
            """
            UPDATE news_notification_work SET attempts = %s, next_attempt_at_ms = %s, updated_at_ms = %s
             WHERE event_id = %s AND channel = %s
            """,
            (spent, int(now_ms) + _retry_delay(NOTIFICATION_RETRY_MS, spent), int(now_ms), event_id, channel),
        )
        return True

    def pending_notification_event_ids(self, *, channel: str, now_ms: int, limit: int) -> list[str]:
        rows = self.conn.execute(
            """
            SELECT event_id FROM news_notification_work
             WHERE channel = %s AND state = 'pending' AND attempts < %s AND next_attempt_at_ms <= %s
             ORDER BY next_attempt_at_ms, event_id
             LIMIT %s
            """,
            (channel, NOTIFICATION_ATTEMPTS_MAX, int(now_ms), int(limit)),
        ).fetchall()
        return [str(row["event_id"]) for row in rows]

    def notification_work(self, event_id: str, channel: str = NEWS_CHANNEL) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM news_notification_work WHERE event_id = %s AND channel = %s", (event_id, channel)
        ).fetchone()
        return None if row is None else dict(row)

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
    def judgment_cache_answer(self, cache_key: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT answer FROM news_judgment_cache WHERE cache_key = %s", (cache_key,)).fetchone()
        return None if row is None else dict(row["answer"])

    def put_judgment_cache_answer(self, *, cache_key: str, answer_json: str, now_ms: int) -> bool:
        cursor = self.conn.execute(
            """
            INSERT INTO news_judgment_cache (cache_key, answer, created_at_ms) VALUES (%s, %s::jsonb, %s)
            ON CONFLICT (cache_key) DO NOTHING
            """,
            (cache_key, answer_json, int(now_ms)),
        )
        return bool(cursor.rowcount)

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
    "NEWS_CHANNEL",
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
    "reader_revision_stamp",
    "revision_evidence",
    "select_receipts",
]
