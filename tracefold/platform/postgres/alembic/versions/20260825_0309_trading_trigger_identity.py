"""One in-flight thesis per underlying, and the two upstream stage timestamps (#211).

Two facts the case row could not previously state.

**One pending or running case per underlying.** `trading_cases.primary_source_key` already made a
re-scanned trigger idempotent, and the partial unique index on `trading_orders` already made one
underlying hold one active order. Neither stopped two *different* triggers for the same issuer from
each freezing a thesis and each spending a model call before either reached an order. The partial
unique index below is that invariant, in the same shape as the order one: it holds only while a case
is undecided, so a terminal or no-trade case never blocks a later genuinely new trigger.

Cases already in flight are coalesced onto the newest trigger per underlying before the index is
created, with the same reason the scanner now counts. This is the durable form of "latest wins": the
alternative — failing the migration on pre-existing duplicates — would leave the invariant unenforced
on exactly the databases that need it.

**Two upstream stage timestamps.** `observed_at_ms` is the trigger's *cutoff*, and for a News trigger
that is the verdict's own creation time, so ingest latency collapsed to zero and could not be
reported. `source_observed_at_ms` (when the provider fact was observed) and `trigger_persisted_at_ms`
(when the verdict naming it became durable) are the two stages Trading does not own but has to be able
to measure. Both are nullable: history is not backfilled, and the status report reads only rows that
carry them.

Revision ID: 20260825_0309
Revises: 20260825_0308
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260825_0309"
down_revision = "20260825_0308"
branch_labels = None
depends_on = None

# The reason the runner would have written for these rows on their very next claim anyway: this
# release bumps `TRADING_MANIFEST_VERSION` to v3, so every case frozen under v2 — the coalescing
# winners included — is retired by `_uses_current_news_generation`. Writing a *different* reason here
# would send an operator looking for a newer trigger that won, when it was blocked seconds later too.
# No `policy_decision` and no `decided_at_ms`: nothing decided anything, and a fabricated decision
# instant would feed the new `stage_latency_ms` as if it were a measurement.
COALESCE_REASON = "news_generation_retired"


def upgrade() -> None:
    op.execute("ALTER TABLE trading_cases ADD COLUMN source_observed_at_ms BIGINT")
    op.execute("ALTER TABLE trading_cases ADD COLUMN trigger_persisted_at_ms BIGINT")
    # A redeploy starts the `migrate` one-shot while the previous release's Workers container is still
    # polling every two seconds, so an INSERT committed between the de-duplication and the index build
    # would fail `CREATE UNIQUE INDEX` and roll the whole revision back. `CREATE INDEX CONCURRENTLY`
    # cannot run inside Alembic's transaction, so the lock is what makes this safe: it blocks writers
    # for the duration and admits nothing between the two statements.
    op.execute("LOCK TABLE trading_cases IN SHARE ROW EXCLUSIVE MODE")
    # Exactly one survivor per underlying, chosen by a total order so the index build cannot fail on
    # its own de-duplication. A case that already authored an order outranks a newer one that did not:
    # `_place` commits the order in its own transaction and `_settle` terminalises the case afterwards,
    # so "RUNNING with a live order" is a reachable durable state, and blocking that case would leave
    # the ledger asserting no authorisation for a position reconciliation is still managing.
    op.execute(
        sa.text(
            """
            WITH ranked AS (
              SELECT c.case_id,
                     row_number() OVER (
                       PARTITION BY c.underlying_key
                       ORDER BY (EXISTS (SELECT 1 FROM trading_orders o WHERE o.case_id = c.case_id)) DESC,
                                c.observed_at_ms DESC,
                                c.case_id DESC
                     ) AS position
                FROM trading_cases c
               WHERE c.state IN ('PENDING', 'RUNNING')
            )
            UPDATE trading_cases c
               SET state = 'BLOCKED',
                   policy_reason = :reason,
                   updated_at_ms = floor(extract(epoch FROM clock_timestamp()) * 1000)::bigint
              FROM ranked
             WHERE ranked.case_id = c.case_id
               AND ranked.position > 1
            """
        ).bindparams(reason=COALESCE_REASON)
    )
    op.execute(
        "CREATE UNIQUE INDEX ux_trading_case_in_flight_underlying "
        "ON trading_cases (underlying_key) WHERE state IN ('PENDING', 'RUNNING')"
    )
    # `stage_latency_ms` and `trading cases` both read a bounded recent window of this table, and it is
    # never purged. Without this they seq-scan the whole history on every `trading status`.
    op.execute("CREATE INDEX ix_trading_cases_created ON trading_cases (created_at_ms DESC)")


def downgrade() -> None:
    # The coalescing above is not reversible — a case blocked as `superseded_by_newer_trigger` cannot
    # be told apart afterwards from one blocked for any other reason — and the two stage stamps are
    # audit evidence for decisions that have already been made. `20260823_0300` and `20260825_0308`
    # refuse for the same reason: a rollback that quietly discards case history is worse than a
    # rollback that stops.
    raise RuntimeError("20260825_0309 coalesced case history and cannot be undone by dropping columns")
