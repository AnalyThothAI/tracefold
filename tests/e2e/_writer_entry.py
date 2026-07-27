"""Entrypoint for the e2e writer sidecar process.

Run as:
  python -m tests.e2e._writer_entry --event-id <id> --text <text>

Reads TRACEFOLD_POSTGRES_DSN (and optional TRACEFOLD_E2E_WS_TOKEN, defaults to
"e2e-token") from env. Builds a Runtime via the same bootstrap path
the production app uses (start_collector=False, no upstream WS), then calls
runtime.ingest.ingest_event(event) with a synthetic mention.

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
    ws_token = os.environ.get("TRACEFOLD_E2E_WS_TOKEN", "e2e-token")

    import asyncio

    from tracefold.app.bootstrap import bootstrap
    from tracefold.market import (
        Author,
        Content,
        Source,
        TwitterEvent,
    )
    from tracefold.platform.config.settings import Settings

    settings = Settings(
        ws_token=ws_token,
        storage={"postgres": {"dsn": dsn, "password_file": None}},
    )
    runtime = bootstrap(settings, start_collector=False)

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
    try:
        runtime.ingest.ingest_event(event)
        print(f"INGESTED {args.event_id}", flush=True)
    finally:
        asyncio.run(runtime.aclose())
    return 0


if __name__ == "__main__":
    sys.exit(main())
