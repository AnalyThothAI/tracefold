"""Close every candidate whose compile record predates the embedded run/spend shape.

Issue #193 PR-C moves the optimization and the spend into `CompileRecordV1` as two embedded objects —
`run` and `spend` — replacing eleven fields that restated what the runner's own result already held.
`0305` shipped one day earlier with the flat shape, so any record written between the two lands with a
field set the model now forbids and surfaces at evaluate/shadow/canary time as
`news_learning_program_compile_record_invalid`: corruption, rather than the hard cut it is.

`schema_version` deliberately stays `news_program_compile_record_v1`. It names the *document* — one
trusted compile, whole — and that has not changed; what changed is which fields carry it. A version bump
would imply two readable shapes, and there is exactly one: `extra="forbid"` refuses the old rows outright.
This migration is what makes that refusal legible instead of surprising.

The `program_v7` epoch is again not re-opened. Accepted `news_review_v4` truth does not depend on how a
compile is serialized — the same reason `0304` and `0305` left it alone.

Revision ID: 20260825_0306
Revises: 20260825_0305
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260825_0306"
down_revision = "20260825_0305"
branch_labels = None
depends_on = None

TRIP_REASON = "compile_record_run_spend_embed"


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE news_canary_activations
               SET state = 'tripped',
                   revision = revision + 1,
                   trip_reason = :trip_reason,
                   tripped_at_ms = floor(extract(epoch FROM clock_timestamp()) * 1000)::bigint
             WHERE state IN ('armed', 'active')
            """
        ).bindparams(trip_reason=TRIP_REASON)
    )


def downgrade() -> None:
    raise RuntimeError("20260825_0306 is an irreversible compile-record shape change")
