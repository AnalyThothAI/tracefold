# News Program v5 / schema 0301 rollback image

This directory is an independent emergency-build input. The build context is
created from the pinned #161 source revision, which contains reviewed Program
v5 plus the complete Trading runtime and schema 0300. It then receives the News
schema-0301 migration, the `queue_priority` storage adapter, and a rollback-only
Trading guard in `schema0301.patch`. The guard admits no new candidate and
blocks an undecided case before model/order work; already prepared orders keep
reconciling. Nothing here is copied into or registered by Program v6.

The supported entry point is `make build-news-rollback-image` from the clean
primary `main` checkout after issue #160 has merged.

## Status after #162 PR8-B (schema head 0302)

`make build-news-rollback-image` now **refuses**, and that is the guard working
rather than a defect. It compares the current source's Alembic head with this
profile's `migration_head` (`20260823_0301`); PR8-B added `20260824_0302`, so the
current source has moved past what this bundle was validated against.

The profile is deliberately **not** re-stamped to `0302`. `verify_runtime.py`
runs inside the image and computes `latest_migration_version()` from the pinned
`source_revision` tree, whose head is `0301`; re-stamping the profile would make
the image fail its own self-check.

What this does and does not mean:

- The already-built image is unaffected and still runnable. `0302` is a
  data-only migration (`DDL` operations: 0 — it inserts one
  `news_learning_epochs` row), so the physical schema it expects is unchanged.
- Rebuilding the bundle from a post-PR8-B `main` requires extending it for
  `0302` and re-running the drill first. Do that before relying on a rebuild in
  an incident, not during one.
