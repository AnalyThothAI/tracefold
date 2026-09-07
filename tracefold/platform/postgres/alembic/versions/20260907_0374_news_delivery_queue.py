"""A push Verdict's handoff to Delivery becomes a row this database can hand out (#598 D2).

Migration evidence:

- category: one new table with one partial index and one foreign key. No existing table, view,
  column, constraint, index or row is read or written by this revision
- why_database_must_change: the handoff from Triage to Delivery is a RabbitMQ message today, and a
  message is not a fact this database can answer for. The verdict is committed in one transaction and
  published in another, so the two can disagree -- which is exactly why `news_verdicts.published_at_ms`
  and a Janitor that republishes after 15 s exist at all. Three answers to "is this card still owed":
  the queue, the marker and the repair scan; and the first time a process died between two of them they
  disagreed, which is the whole content of `test_a_verdict_mark_failure_redelivers_the_decision_into_
  one_delivery_lifecycle`.

  One row in one table, written inside the transaction that writes the verdict, is one answer. It is
  the same shape `20260905_0366` already gave the market lane and states in its own words: *not an
  outbox beside a publish flag beside a retry table*. `next_attempt_at_ms` is the whole scheduler --
  a card is claimable when it is due, a retry is a later due time rather than a message the broker
  redelivers -- and `FOR UPDATE SKIP LOCKED` over that column is the claim.

  The table is deliberately small and deliberately not a ledger. `news_deliveries` is unchanged and is
  still the only record of what a reader was sent: this table holds work that is still owed, and a row
  leaves it the moment `begin_delivery` has written the ledger row. What stays is a `dead` row, which
  is this lane's replacement for a `news.dead` message -- an intent that spent its attempt budget, kept
  where an operator can read it with one `SELECT` instead of a broker inspection.
- current_source_revision: 20260906_0373
- minimum_supported_source_revision: 20260906_0373
- lock_level_and_order: one `CREATE TABLE`, which takes no lock on anything that exists except a
  `SHARE ROW EXCLUSIVE` on `news_events` for the moment its primary key is referenced by the foreign
  key; then one index build on the new, empty table
- statement_timeout: 120s set locally by the revision
- lock_timeout: 5s set locally by the revision
- estimated_rows: the table starts empty and stays empty in the steady state. Production settles about
  300-500 push verdicts a day and each row is deleted within seconds of being claimed, so the live set
  is the number of cards in flight -- measured peak 7 per minute on `news.deliver`, the queue this
  replaces
- estimated_bytes: single-digit kilobytes at that row count; one partial index over the same rows
- rewrite_or_index_build: nothing is rewritten. The one index is built on an empty table
- preflight_and_maintenance_boundary: writers must be stopped, and `make up` is that boundary. Triage
  on the new code writes a queue row inside the verdict transaction, and a Deliverer on the old code
  would never read it; a Triage on the old code publishes to `news.deliver`, and a Deliverer on the
  new code would never receive it. The two halves must not run against each other, which is what
  stopping Workers for the migration guarantees.

  One operator step goes with it, and it is recorded in `docs/OPERATIONS.md`: a push Verdict settled by
  the old code whose card had not been delivered when Workers stopped has no queue row, because the old
  code did not write one. With Workers still down and this revision applied, the operator seeds those
  rows with the documented statement -- the same relevance window the Janitor repaired, and no wider,
  so a card nobody could still act on is not sent hours late. It is an operator statement rather than
  part of this revision on purpose: this revision reads no other table, and the campaign's constraint
  is that no migration touches `news_verdicts` or `news_deliveries` at all.
- archive_current_compatibility: no existing row is read or written. Nothing in the archive changes
  shape, and every existing reader of every existing table sees exactly what it saw before
- role_and_grant_impact: none; the single `tracefold` login is unchanged
- failure_state: the transaction rolls back completely and the database keeps its current shape
- roll_forward_or_verified_backup_restore: `downgrade` drops the table, which is the honest reverse of
  creating it: the rows in it are work still owed, not evidence of what a reader received -- that is
  `news_deliveries`, which this revision does not touch. A rollback to an image that still publishes to
  `news.deliver` therefore loses only the in-flight to-do list, and the same operator statement that
  seeds it forward re-derives it
- production_postgres_image:
  postgres:18-bookworm@sha256:1961f96e6029a02c3812d7cb329a3b03a3ac2bb067058dec17b0f5596aca9296

Revision ID: 20260907_0374
Revises: 20260906_0373
Create Date: 2026-09-07 00:00:00
"""

from __future__ import annotations

from alembic import op

revision = "20260907_0374"
down_revision = "20260906_0373"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '120s'")

    _delivery_queue()


def _delivery_queue() -> None:
    """One row per card still owed to a reader: the due time, the attempt count, and nothing else.

    There is no card, no receipt and no delivered state here. Those are `news_deliveries`, and putting
    a second copy of them beside it is how two tables come to disagree about what a reader was sent.

    `attempts` is bounded by the same three the broker policy gave this lane (`delivery-limit: 2`
    delivers three times), so the column cannot hold a fourth and the loop cannot quietly grant one.
    """

    op.execute(
        """
        CREATE TABLE public.news_delivery_queue (
            event_id text NOT NULL,
            kind text NOT NULL,
            state text NOT NULL DEFAULT 'pending',
            attempts integer NOT NULL DEFAULT 0,
            error_code text,
            enqueued_at_ms bigint NOT NULL,
            next_attempt_at_ms bigint NOT NULL,
            last_attempt_at_ms bigint,
            settled_at_ms bigint,
            updated_at_ms bigint NOT NULL,
            CONSTRAINT news_delivery_queue_pkey PRIMARY KEY (event_id, kind),
            CONSTRAINT news_delivery_queue_event_id_fkey
                FOREIGN KEY (event_id) REFERENCES public.news_events(event_id) ON DELETE CASCADE,
            -- The same two kinds `news_deliveries` names, because a queue row and its ledger row are
            -- the same intent at two moments and are keyed identically.
            CONSTRAINT news_delivery_queue_kind_check
                CHECK (kind = ANY (ARRAY['first'::text, 'followup'::text])),
            -- Two states and no third. `pending` is work still owed; `dead` is an intent that spent
            -- its attempts, kept as this lane's replacement for a `news.dead` message. There is no
            -- `sent`: a delivered card leaves this table, and `news_deliveries` says what happened.
            CONSTRAINT news_delivery_queue_state_check
                CHECK (state = ANY (ARRAY['pending'::text, 'dead'::text])),
            CONSTRAINT news_delivery_queue_attempts_check
                CHECK (attempts >= 0 AND attempts <= 3),
            CONSTRAINT news_delivery_queue_settled_check
                CHECK ((settled_at_ms IS NOT NULL) = (state = 'dead'::text)),
            CONSTRAINT news_delivery_queue_attempted_check
                CHECK ((attempts = 0) = (last_attempt_at_ms IS NULL))
        )
        """
    )
    # The claim predicate, and the only index it needs. Ordered by due time so the oldest owed card is
    # claimed first, with the enqueue stamp and the key as the tie-breakers a claim must have to be
    # deterministic under two claimers. Partial on `pending`, so a `dead` row an operator has not yet
    # read costs the claim nothing.
    op.execute(
        """
        CREATE INDEX ix_news_delivery_queue_due
            ON public.news_delivery_queue (next_attempt_at_ms, enqueued_at_ms, event_id)
         WHERE state = 'pending'
        """
    )


def downgrade() -> None:
    """Drop the to-do list. It is work still owed, never the record of what a reader received."""

    op.execute("DROP TABLE public.news_delivery_queue")
