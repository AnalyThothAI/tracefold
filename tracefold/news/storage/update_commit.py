"""One short, Event-serialized commit for semantic updates and proven scope repairs."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from ..updates.contracts import NOTIFICATION_CHANGES, EventUpdate
from .sql_values import _dumps
from .trade_projection import TradeProjectionStorage

if TYPE_CHECKING:
    from ..claim_recall import Probe

# The public kind of a PublicUpdate in the News outbox. A catalyst delta keeps the existing kind.
PUBLIC_TRADE_KINDS: Final[dict[str, str]] = {"catalyst_delta": "catalyst", "source_update": "source_update"}


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
    conn.execute("SELECT event_id FROM news_events WHERE event_id=%s FOR NO KEY UPDATE", (event_id,))


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
    probes: Mapping[str, Probe] | None = None,
) -> bool:
    """Write the source, immutable analysis adoption, head pointer, public outbox and work in one transaction.

    Every comparison remains in the adopted analysis's changes, so a later revision cannot lose it (#742).

    The caller has already taken the Event lock and checked its source under that lock.
    Only an update with a notification change (new content, a real change, a correction or a
    conflict) opens or resets notification work. Any other update -- a restatement, a new source
    for an adopted claim, a pure scope retraction -- only retargets work still owed (pending, or
    failed and waiting for an operator retry) to the new head; it does not create work, reopen a
    failed one or replenish the budget of any responsibility.
    """

    event_id = update.event_id
    head = conn.execute(
        "SELECT a.update_ref,a.input_revision FROM news_events e JOIN news_analyses a "
        "ON a.analysis_id=e.current_analysis_id WHERE e.event_id=%s",
        (event_id,),
    ).fetchone()
    if (None if head is None else str(head["update_ref"])) != expected_head_ref:
        return False
    if head is not None and update.input_revision < int(head["input_revision"]):
        raise ValueError("news_update_input_revision_downgrade")
    if isinstance(source, ScopeProofSource):
        analysis_id = source.repair_id
        inserted = conn.execute(
            """INSERT INTO news_analyses(analysis_id,event_id,origin,input_revision,completed_at_ms,repair,
                 content_revision,previous_content_revision,update_ref,adopted_at_ms,document)
               VALUES (%s,%s,'scope_repair',%s,%s,%s::jsonb,%s,%s,%s,%s,%s::jsonb)
               ON CONFLICT (analysis_id) DO NOTHING RETURNING analysis_id""",
            (
                analysis_id,
                event_id,
                update.input_revision,
                source.recorded_at_ms,
                _dumps(
                    {
                        "claim_refs": source.claim_refs,
                        "proof": source.proof,
                        "projection_version": source.projection_version,
                    }
                ),
                update.content_revision,
                update.previous_content_revision,
                update.ref,
                update.adopted_at_ms,
                document_json,
            ),
        ).fetchone()
    else:
        analysis_id = source.observation_result_id
        inserted = conn.execute(
            """UPDATE news_analyses SET content_revision=%s,previous_content_revision=%s,update_ref=%s,
                 adopted_at_ms=%s,document=%s::jsonb
               WHERE analysis_id=%s AND event_id=%s AND input_revision=%s AND origin='semantic'
                 AND adopted_at_ms IS NULL RETURNING analysis_id""",
            (
                update.content_revision,
                update.previous_content_revision,
                update.ref,
                update.adopted_at_ms,
                document_json,
                analysis_id,
                event_id,
                update.input_revision,
            ),
        ).fetchone()
    if inserted is None:
        raise ValueError("news_event_update_revision_exists")
    from .claim_index import ClaimIndexStorage

    ClaimIndexStorage(conn).index_update(update, probes=probes)
    conn.execute("UPDATE news_events SET current_analysis_id=%s WHERE event_id=%s", (analysis_id, event_id))
    for kind, payload in public_rows:
        if not outbox.enqueue_trade_event(
            kind=kind,
            source_fact_key=event_id,
            source_revision=update.content_revision,
            payload=payload,
            source_recorded_at_ms=int(payload["semantic_completed_at_ms"]),
        ):
            raise ValueError("news_public_update_conflict")
    from .notification_jobs import NotificationJobs

    NotificationJobs(conn).open_or_retarget(
        event_id,
        update.content_revision,
        reopen=any(change.kind in NOTIFICATION_CHANGES for change in update.changes),
        now_ms=now_ms,
    )
    return True
