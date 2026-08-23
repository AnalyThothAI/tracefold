# News Program v5 / schema 0300 rollback image

This directory is an independent emergency-build input.  The build context is
created from the pinned, reviewed Program v5 source revision, then receives only
the schema-0300 migration and `queue_priority` storage adapter in
`schema0300.patch`.  Nothing in this directory is copied into or registered by
the Program v6 runtime.

The supported entry point is `make build-news-rollback-image` from the clean
primary `main` checkout after issue #160 has merged.
