"""One short, Event-serialized commit for semantic updates and proven scope repairs."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from ..notifications.contracts import NEWS_CHANNEL
from ..updates.contracts import NOTIFICATION_CHANGES, EventUpdate
from .sql_values import _dumps
from .trade_projection import TradeProjectionStorage

# The public kind of a PublicUpdate in the News outbox. A catalyst delta keeps the existing kind.
# The relations a notification reader can act on; `unrelated` and `unresolved` are not links.
CLAIM_LINK_RELATIONS: frozenset[str | None] = frozenset(
    {"equivalent", "adds_information", "real_world_change", "corrects", "conflicts"}
)
PUBLIC_TRADE_KINDS: Final[dict[str, str]] = {"catalyst_delta": "catalyst", "source_update": "source_update"}
_EVENT_LOCK_NAMESPACE = 0x4E455755  # All head, plan and send-permission writers use this lock.


@dataclass(frozen=True)
class SemanticSource:
    observation_result_id: str


@dataclass(frozen=True)
class ScopeProofSource:
    repair_id: str
    previous_content_revision: str
    claim_refs: tuple[str, ...]
    proof: Mapping[str, Any]
    projection_version: str
    recorded_at_ms: int


def lock_event(conn: Any, event_id: str) -> None:
    """Acquire before touching head, work, queue or ledger rows in a write transaction."""

    conn.execute("SET LOCAL lock_timeout = '2500ms'")
    conn.execute("SELECT pg_advisory_xact_lock(%s, hashtext(%s))", (_EVENT_LOCK_NAMESPACE, event_id))


def commit_update(
    conn: Any,
    *,
    outbox: TradeProjectionStorage,
    expected_head_ref: str | None,
    update: EventUpdate,
    document_json: str,
    source: SemanticSource | ScopeProofSource,
    public_rows: Sequence[tuple[str, Mapping[str, Any]]],
    now_ms: int,
    prior_events: Mapping[str, str] | None = None,
) -> bool:
    """Write the source, immutable update, its claim links, head, public outbox and work in one transaction.

    Every change that compares a claim with an earlier one is kept as a claim link, so a later revision that
    no longer repeats the comparison cannot lose it (#742). `prior_events` names the Event of each prior
    claim the semantic input supplied.

    The caller has already taken the Event lock and checked its source under that lock.
    Only an update with a notification change (new content, a real change, a correction or a
    conflict) opens or resets notification work. Any other update -- a restatement, a new source
    for an adopted claim, a pure scope retraction -- only retargets work still owed (pending, or
    failed and waiting for an operator retry) to the new head; it does not create work, reopen a
    failed one or replenish the budget of any responsibility.
    """

    event_id = update.event_id
    head = conn.execute(
        "SELECT update_ref,input_revision FROM news_event_update_heads WHERE event_id=%s", (event_id,)
    ).fetchone()
    if (None if head is None else str(head["update_ref"])) != expected_head_ref:
        return False
    if head is not None and update.input_revision < int(head["input_revision"]):
        raise ValueError("news_update_input_revision_downgrade")
    if isinstance(source, ScopeProofSource):
        conn.execute(
            """INSERT INTO news_head_scope_repairs
                 (repair_id,event_id,previous_content_revision,content_revision,claim_refs,
                  proof,projection_version,recorded_at_ms)
               VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s,%s)""",
            (
                source.repair_id,
                event_id,
                source.previous_content_revision,
                update.content_revision,
                list(source.claim_refs),
                _dumps(source.proof),
                source.projection_version,
                source.recorded_at_ms,
            ),
        )
    inserted = conn.execute(
        """INSERT INTO news_event_updates
             (event_id,content_revision,input_revision,previous_content_revision,adopted_at_ms,
              observation_result_id,scope_repair_id,document)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
           ON CONFLICT (event_id,content_revision) DO NOTHING RETURNING content_revision""",
        (
            event_id,
            update.content_revision,
            update.input_revision,
            update.previous_content_revision,
            update.adopted_at_ms,
            source.observation_result_id if isinstance(source, SemanticSource) else None,
            source.repair_id if isinstance(source, ScopeProofSource) else None,
            document_json,
        ),
    ).fetchone()
    if inserted is None:
        raise ValueError("news_event_update_revision_exists")
    own = {claim.ref for claim in update.claims}
    for change in update.changes:
        if change.previous_ref is None or change.previous_ref == change.current_ref:
            continue
        if change.relation not in CLAIM_LINK_RELATIONS:
            continue
        conn.execute(
            """INSERT INTO news_claim_links
                 (update_ref,current_ref,previous_ref,relation,current_event_id,previous_event_id,asserted_at_ms)
               VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
            (
                update.ref,
                change.current_ref,
                change.previous_ref,
                change.relation,
                event_id,
                event_id if change.previous_ref in own else (prior_events or {}).get(change.previous_ref),
                update.adopted_at_ms,
            ),
        )
    conn.execute(
        """INSERT INTO news_event_update_heads
             (event_id,content_revision,input_revision,update_ref,adopted_at_ms)
           VALUES (%s,%s,%s,%s,%s)
           ON CONFLICT (event_id) DO UPDATE SET
             content_revision=EXCLUDED.content_revision,
             input_revision=GREATEST(news_event_update_heads.input_revision,EXCLUDED.input_revision),
             update_ref=EXCLUDED.update_ref, adopted_at_ms=EXCLUDED.adopted_at_ms""",
        (event_id, update.content_revision, update.input_revision, update.ref, update.adopted_at_ms),
    )
    for kind, payload in public_rows:
        if not outbox.enqueue_trade_event(
            kind=kind,
            source_fact_key=event_id,
            source_revision=update.content_revision,
            payload=payload,
            source_recorded_at_ms=int(payload["semantic_completed_at_ms"]),
        ):
            raise ValueError("news_public_update_conflict")
    if not any(change.kind in NOTIFICATION_CHANGES for change in update.changes):
        conn.execute(
            """UPDATE news_notification_work
                  SET content_revision=%s,decision_ref=NULL,reader_revision=NULL,
                      next_attempt_at_ms=LEAST(next_attempt_at_ms,%s)
                WHERE event_id=%s AND channel=%s AND state IN ('pending','failed')""",
            (update.content_revision, int(now_ms), event_id, NEWS_CHANNEL),
        )
    else:
        conn.execute(
            """INSERT INTO news_notification_work
                 (event_id,channel,content_revision,state,attempts,next_attempt_at_ms,updated_at_ms)
               VALUES (%s,%s,%s,'pending',0,%s,%s)
               ON CONFLICT (event_id,channel) DO UPDATE SET
                 content_revision=EXCLUDED.content_revision,state='pending',attempts=0,
                 last_error_code=NULL,decision_ref=NULL,reader_revision=NULL,
                 next_attempt_at_ms=EXCLUDED.next_attempt_at_ms,updated_at_ms=EXCLUDED.updated_at_ms""",
            (event_id, NEWS_CHANNEL, update.content_revision, int(now_ms), int(now_ms)),
        )
    return True
