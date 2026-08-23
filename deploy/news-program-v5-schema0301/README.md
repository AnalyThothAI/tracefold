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
