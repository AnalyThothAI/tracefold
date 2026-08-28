"""Let the running deployment open its own evidence epoch (#314).

Epochs `program_v1` through `program_v9` were opened by nine hand-written migrations, each one authored by
whoever noticed that an identity had moved. That is the whole failure mode: #313's bump was found complete
only because CI held a bare `== 8` nobody had named, and two of the three identity-clearing incidents were
deployments whose authors did not know they had changed behavior at all.

An epoch is now keyed to the bundle that accrues evidence under it — the two instructions, the computed
execution envelope, the four model slots, the retrieval contract and the policy — and opened by the worker
startup barrier the first time it sees one. This migration gives the table the two columns that identity
needs and grants `tracefold_workers` the INSERT to append with. UPDATE and DELETE stay revoked and the
append-only trigger stays in place, so a runtime writer can add history and still cannot rewrite it.

`program_factory_id` becomes nullable rather than dropped: the nine historical rows name the factory they
were opened under, and that is true of them.

Revision ID: 20260828_0321
Revises: 20260828_0320
"""

from __future__ import annotations

from alembic import op

revision = "20260828_0321"
down_revision = "20260828_0320"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE news_learning_epochs
          ADD COLUMN bundle_sha      text,
          ADD COLUMN envelope_sha256 text,
          ALTER COLUMN program_factory_id DROP NOT NULL
        """
    )
    # Partial-unique, not plain unique: every historical row is NULL here and two NULLs do not collide in
    # PostgreSQL, but being explicit is what keeps a future migration from reading this as "bundles may
    # repeat".
    op.execute(
        "CREATE UNIQUE INDEX news_learning_epochs_bundle_sha_key "
        "ON news_learning_epochs (bundle_sha) WHERE bundle_sha IS NOT NULL"
    )
    op.execute(
        """
        ALTER TABLE news_learning_epochs
          ADD CONSTRAINT news_learning_epoch_bundle_sha
            CHECK (bundle_sha IS NULL OR bundle_sha ~ '^[0-9a-f]{64}$'),
          ADD CONSTRAINT news_learning_epoch_envelope_sha
            CHECK (envelope_sha256 IS NULL OR envelope_sha256 ~ '^[0-9a-f]{64}$'),
          ADD CONSTRAINT news_learning_epoch_runtime_identity
            CHECK ((bundle_sha IS NULL) = (envelope_sha256 IS NULL))
        """
    )
    op.execute("GRANT INSERT ON news_learning_epochs TO tracefold_workers")
    op.execute("REVOKE UPDATE, DELETE ON news_learning_epochs FROM tracefold_workers")


def downgrade() -> None:
    raise RuntimeError("20260828_0321 is an irreversible runtime-owned learning epoch cut")
