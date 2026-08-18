from __future__ import annotations

import argparse


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _nonnegative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be a non-negative integer")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tracefold")
    subcommands = parser.add_subparsers(dest="command")

    subcommands.add_parser("serve", help="run the read-only HTTP, frontend, and WebSocket runtime")
    subcommands.add_parser("workers", help="run the ingestion, projection, provider, and model runtime")

    init = subcommands.add_parser("init", help="create ~/.tracefold/config.yaml")
    init.add_argument("--force", action="store_true", help="overwrite existing config.yaml")

    subcommands.add_parser("config", help="print effective runtime configuration")

    db = subcommands.add_parser("db", help="database lifecycle commands")
    db_subcommands = db.add_subparsers(dest="db_command", required=True)
    db_subcommands.add_parser("migrate", help="apply PostgreSQL migrations")
    db_subcommands.add_parser("health", help="check PostgreSQL liveness and migration version")
    db_subcommands.add_parser("audit", help="run PostgreSQL count, FK, and projection schema audit")
    query_audit = db_subcommands.add_parser("query-audit", help="explain PostgreSQL hot read paths")
    query_audit.add_argument("--analyze", action="store_true", help="run EXPLAIN ANALYZE with buffers")

    macro = subcommands.add_parser("macro", help="Macro acquisition and current-module commands")
    macro_subcommands = macro.add_subparsers(dest="macro_command", required=True)
    macro_backfill = macro_subcommands.add_parser("backfill", help="execute an explicit dataset backfill")
    macro_backfill.add_argument("--dataset", required=True, help="Dataset Registry id")
    macro_backfill.add_argument("--start", required=True, help="history start date (YYYY-MM-DD)")
    macro_backfill.add_argument("--end", required=True, help="history end date (YYYY-MM-DD)")
    macro_subcommands.add_parser(
        "backfill-professional",
        help="execute the code-owned professional Macro history policy",
    )
    macro_subcommands.add_parser(
        "status",
        help="print acquisition and current-module status",
    )

    news = subcommands.add_parser("news", help="News V3 broker, control, and evaluation commands")
    news_subcommands = news.add_subparsers(dest="news_command", required=True)
    news_subcommands.add_parser(
        "bus-check", help="connect to RabbitMQ, declare the News topology, and print queue depths"
    )
    news_control = news_subcommands.add_parser("control", help="publish a control command to all News consumers")
    news_control.add_argument(
        "action",
        choices=("pause_delivery", "resume_delivery", "mute_theme", "mute_symbol", "unmute", "drain"),
    )
    news_control.add_argument("--key", default="", help="theme name or symbol for mute/unmute")
    news_control.add_argument("--ttl-minutes", type=_positive_int, default=360, help="mute duration")
    news_eval = news_subcommands.add_parser(
        "eval", help="offline evaluation of Triage decisions against market marks and labels"
    )
    news_eval.add_argument("--hours", type=_positive_int, default=168, help="look-back window")
    news_eval.add_argument("--policy-version", default="", help="restrict to one triage policy version")
    news_replay = news_subcommands.add_parser(
        "replay", help="replay a JSON file of provider hits through Deduper+Gate (no model, no broker)"
    )
    news_replay.add_argument("path", help="JSON file: {strategy_id: [hit, ...]} or [hit, ...]")

    recent = subcommands.add_parser("recent", help="print recent stored events")
    recent.add_argument("--limit", type=_positive_int, default=20)
    recent.add_argument("--handles", default="")
    recent.add_argument("--ca", default="", help="filter by token contract address")
    recent.add_argument("--chain", default="", help="chain for contract address filters")
    recent.add_argument("--symbol", default="", help="filter by cashtag symbol")

    search = subcommands.add_parser("search", help="search stored tweets by query text")
    search.add_argument("query", nargs="?", default="")
    search.add_argument("--window", choices=("5m", "1h", "4h", "24h"), default="24h")
    search.add_argument("--limit", type=_positive_int, default=20)
    search.add_argument("--cursor", default="", help="opaque cursor returned by a prior search page")

    ops = subcommands.add_parser("ops", help="maintenance commands")
    ops_subcommands = ops.add_subparsers(dest="ops_command", required=True)
    seal_acceptance = ops_subcommands.add_parser(
        "seal-workers-runtime-acceptance",
        help="validate and seal a complete Workers Runtime V2 acceptance bundle",
    )
    seal_acceptance_mode = seal_acceptance.add_mutually_exclusive_group(
        required=True,
    )
    seal_acceptance_mode.add_argument(
        "--bundle",
        help="directory containing evidence.json and supporting evidence files",
    )
    seal_acceptance_mode.add_argument(
        "--template",
        action="store_true",
        help="print a deliberately non-passing evidence.json template",
    )
    collect_acceptance = ops_subcommands.add_parser(
        "collect-workers-runtime-acceptance",
        help="collect the fixed 30-minute production Workers Runtime V2 interval",
    )
    collect_acceptance.add_argument(
        "--bundle",
        required=True,
        help="new absolute evidence directory outside the repository checkout",
    )
    rebuild_market_current = ops_subcommands.add_parser(
        "rebuild-market-current",
        help="rebuild current market rows from persisted market tick facts",
    )
    rebuild_market_current.add_argument("--after-target-type", default="")
    rebuild_market_current.add_argument("--after-target-id", default="")
    rebuild_market_current.add_argument("--limit", type=_positive_int, default=500)
    rebuild_market_current.add_argument("--execute", action="store_true", required=True)
    queue_inspect = ops_subcommands.add_parser("queue-inspect", help="inspect worker queue terminal evidence")
    queue_inspect.add_argument("--owner", default="")
    queue_inspect.add_argument("--source-table", default="")
    queue_inspect.add_argument("--status", choices=("terminal", "active"), default="terminal")
    queue_inspect.add_argument("--reason-bucket", default="")
    queue_inspect.add_argument("--limit", type=_positive_int, default=50)
    queue_resolve = ops_subcommands.add_parser("queue-resolve", help="resolve worker queue terminal evidence")
    queue_resolve.add_argument("--terminal-id", required=True)
    queue_resolve.add_argument("--action", choices=("retry", "quarantine", "archive"), required=True)
    queue_resolve.add_argument("--reason", required=True)
    queue_resolve.add_argument("--execute", action="store_true", required=True)
    queue_resolve_bucket = ops_subcommands.add_parser(
        "queue-resolve-bucket",
        help="resolve a bounded worker queue terminal evidence bucket",
    )
    queue_resolve_bucket.add_argument("--owner", required=True)
    queue_resolve_bucket.add_argument("--source-table", required=True)
    queue_resolve_bucket.add_argument("--reason-bucket", required=True)
    queue_resolve_bucket.add_argument("--action", choices=("retry", "quarantine", "archive"), required=True)
    queue_resolve_bucket.add_argument("--reason", required=True)
    queue_resolve_bucket.add_argument("--limit", type=_positive_int, default=100)
    queue_resolve_bucket_mode = queue_resolve_bucket.add_mutually_exclusive_group(required=True)
    queue_resolve_bucket_mode.add_argument("--dry-run", action="store_true")
    queue_resolve_bucket_mode.add_argument("--execute", action="store_true")
    reconcile_event_anchor = ops_subcommands.add_parser(
        "reconcile-event-anchor-jobs",
        help="one-shot reconcile of historical ready event-anchor backfill jobs",
    )
    reconcile_event_anchor.add_argument("--limit", type=_positive_int, default=1000)
    reconcile_event_anchor.add_argument("--execute", action="store_true")
    validate_projections = ops_subcommands.add_parser(
        "validate-projections",
        help="validate projection read models against PostgreSQL facts",
    )
    validate_projections.add_argument("--sample", type=_nonnegative_int, default=100)
    sync_binance_universe = ops_subcommands.add_parser(
        "sync-binance-usdt-perp-universe",
        help="sync Binance USD-M USDT perpetual contracts into the CEX registry",
    )
    sync_binance_universe_mode = sync_binance_universe.add_mutually_exclusive_group(required=True)
    sync_binance_universe_mode.add_argument("--dry-run", action="store_true")
    sync_binance_universe_mode.add_argument("--execute", action="store_true")
    ops_subcommands.add_parser("sync-binance-cex-profiles", help="sync Binance CEX token profiles")
    ops_subcommands.add_parser("sync-us-equity-symbols", help="sync Nasdaq Trader US equity symbols")
    run_resolution_refresh = ops_subcommands.add_parser(
        "run-resolution-refresh",
        help="refresh due token resolution lookups and reprocess recent intents",
    )
    run_resolution_refresh.add_argument("--limit", type=_positive_int, default=50)
    run_resolution_refresh.add_argument("--reprocess-limit", type=_positive_int, default=500)
    refresh_asset_profiles = ops_subcommands.add_parser(
        "refresh-asset-profiles",
        help="enqueue missing DEX profile targets and refresh due profile facts",
    )
    refresh_asset_profiles.add_argument("--limit", type=_positive_int, default=50)
    mirror_token_images = ops_subcommands.add_parser(
        "mirror-token-images",
        help="mirror provider token images into the local cache",
    )
    mirror_token_images.add_argument("--limit", type=_positive_int, default=500)
    reprocess_token_intents = ops_subcommands.add_parser(
        "reprocess-token-intents",
        help="re-resolve recent unresolved token intents",
    )
    reprocess_token_intents.add_argument("--window", choices=("5m", "1h", "4h", "24h"), default="24h")
    reprocess_token_intents.add_argument("--limit", type=_positive_int, default=500)
    reprocess_token_intents.add_argument("--lookup-key", action="append", default=[])
    rebuild_token_intents = ops_subcommands.add_parser(
        "rebuild-token-intents",
        help="rebuild recent token evidence, intents, resolutions, and lookup keys",
    )
    rebuild_token_intents.add_argument("--window", choices=("5m", "1h", "4h", "24h"), default="24h")
    rebuild_token_intents.add_argument("--limit", type=_positive_int, default=500)
    audit_token_intent = ops_subcommands.add_parser(
        "audit-token-intent",
        help="inspect token intent evidence and resolution",
    )
    audit_token_intent_target = audit_token_intent.add_mutually_exclusive_group(required=True)
    audit_token_intent_target.add_argument("--event-id", default="")
    audit_token_intent_target.add_argument("--intent-id", default="")
    return parser
