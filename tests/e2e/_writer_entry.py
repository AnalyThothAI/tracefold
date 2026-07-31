"""Entrypoint for the e2e writer sidecar process.

Run as:
  python -m tests.e2e._writer_entry --event-id <id> --text <text>

Reads TRACEFOLD_POSTGRES_DSN from env, opens the production workers database
boundary, then commits a synthetic mention through the same ingestion service
used by the collector.

By writing through the production wiring chain we exercise the same
EvidenceRepository -> events table path the API will read from.

Stdout: 'INGESTED <event_id>'. Exit 0 on success.
"""

from __future__ import annotations

import argparse
import os
import sys
import time


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--event-id", required=True)
    parser.add_argument("--text", required=True)
    parser.add_argument("--author", default="e2e_test")
    args = parser.parse_args()

    dsn = os.environ.get("TRACEFOLD_POSTGRES_DSN")
    if not dsn:
        print("FATAL: TRACEFOLD_POSTGRES_DSN not set", file=sys.stderr)
        return 1
    from tracefold.app.database import WorkerDatabase
    from tracefold.app.workers import _ingest_service_for_repos
    from tracefold.market import (
        Author,
        Content,
        Source,
        TwitterEvent,
    )
    from tracefold.platform.config.settings import Settings

    settings = Settings(
        storage={
            "postgres": {
                "serve_dsn": dsn,
                "workers_dsn": dsn,
                "migrate_dsn": dsn,
                "serve_password_file": None,
                "workers_password_file": None,
                "migrate_password_file": None,
            }
        },
    )
    received_at_ms = int(time.time() * 1000)
    event = TwitterEvent(
        event_id=args.event_id,
        source=Source(
            provider="gmgn",
            transport="direct_ws",
            coverage="public_stream",
            channel="twitter_monitor_basic",
        ),
        action="tweet",
        original_action=None,
        tweet_id=args.event_id,
        internal_id=args.event_id,
        timestamp=received_at_ms // 1000,
        received_at_ms=received_at_ms,
        author=Author(handle=args.author, name=args.author, avatar=None, followers=100, tags=[]),
        content=Content(text=args.text, media=[]),
        reference=None,
        unfollow_target=None,
        avatar_change=None,
        bio_change=None,
        raw={"id": args.event_id},
    )
    db = WorkerDatabase.create(settings)
    try:
        with db.worker_session("collector") as repos:
            ingest = _ingest_service_for_repos(
                repos,
                event_anchor_active_window_ms=300_000,
            )
            ingest.ingest_event(event)
        print(f"INGESTED {args.event_id}", flush=True)
    finally:
        db.worker_pool.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
