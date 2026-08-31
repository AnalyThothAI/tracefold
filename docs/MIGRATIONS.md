# PostgreSQL migrations

PostgreSQL plus Alembic's single head is the only schema authority. Runtime
processes never execute DDL.

## Current baseline

`20260831_0340` is both the single Alembic root and head. It is a reviewed
current-schema baseline with `down_revision = None`; a fresh PostgreSQL 18
database reaches the complete current schema in one step. The baseline creates
application tables, sequences, views, indexes, functions, triggers,
constraints, and only the structural singleton rows required on an empty
cluster. Extensions remain the empty-PGDATA bootstrap's responsibility.

This squash is the operator-authorized exception recorded by issue #449. The
supported pre-cut database was first advanced to exact old terminal head
`20260831_0340`. The terminal identity was then reused while revisions
`20260818_0275` through `20260831_0339`, the old contents of `0340`, and the
role/ACL bootstrap SQL were removed. An already-stamped database therefore
does not replay baseline DDL or rewrite business rows. Git history and the
recorded pre-cut image remain the recovery authority for a pre-baseline backup.

The stopped-writer one-time catalog cut renamed `tracefold_owner` to
`tracefold`, removed the Serve/Workers/Nautilus roles and their ACL/default-ACL
entries, and preserved the terminal revision, normalized schema fingerprint,
row counts, and business identity aggregates. Its mismatch preflight ran before
catalog writes. The helper was deleted after the receipt; current source has no
old-role repair, fallback, dual head, or compatibility path.

## Authoring contract

After the baseline, every real schema or durable-data change is a new linear,
forward-only revision. Published revisions are immutable; a correction is a
new revision. A revision is warranted only for a table, column, view, index,
function, trigger, constraint, or durable-data change—not for Python
refactors, timeout values, presentation, or application identities.

Every revision records:

- why PostgreSQL must change and the current source revision;
- lock level/order, statement and lock timeouts, estimated rows/bytes, and
  rewrite or index-build behavior;
- preflight, maintenance boundary, failure state, and roll-forward or verified
  backup-restore path;
- archive/current compatibility and the exact PostgreSQL 18 image used by its
  evidence.

Required evidence is a fresh database to head, head-to-head no-op, the smallest
historical fixture affected, and bounded real-PostgreSQL checks for destructive,
locking, or backfill behavior. Business processes remain behind the maintenance
gate until migration succeeds. An irreversible downgrade is a verified backup
restore, never invented reverse DDL.

## Database development standard

1. The deployment has one non-superuser application login, `tracefold`.
   Process categories use stable `application_name` values, not database roles
   or ACL matrices.
2. Connections default to autocommit. Use a short explicit transaction only
   for an atomic write or a stated consistent snapshot; keep provider, model,
   file, broker, large-JSON, and hashing work outside it.
3. One production statement has one owner. Runtime, audit, and tests reuse that
   statement or builder instead of carrying approximate copies.
4. Bind values. Compose dynamic identifiers with `psycopg.sql`.
5. Page, claim, purge, and backfill operations have a hard limit, deterministic
   order, tie-breaker, and maximum transaction/payload budget.
6. Natural PK/UNIQUE identities plus `ON CONFLICT` or conditional writes own
   idempotency; do not implement check-then-write races.
7. Keep cross-process, cross-table, economic-state, and append-only database
   invariants. Typed application models normally own single-process payload
   shape; internal permission denial is not a business rule.
8. Use typed columns for query, join, and order keys. JSONB is for bounded
   payloads that must be versioned as a whole; do not duplicate the same truth
   in scalar columns and an unconstrained payload.
9. A new index names its production query, predicate/order, measured scale,
   and write/storage cost. Zero scans are deletion evidence only after a reset
   and a complete representative business window.
10. Performance claims compare the same revision, configuration, parameters,
    and workload window across application, pool, and PostgreSQL evidence.
11. Every view, trigger, function, gate, timeout, index, and projection names a
    current correctness or measured-performance owner. Otherwise remove it.
12. A new database role first proves a distinct trust domain. A process name is
    not sufficient justification.
