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

    subcommands.add_parser("serve", help="run the read-only HTTP and frontend runtime")
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

    news = subcommands.add_parser("news", help="News V3 broker, control, label, and evaluation commands")
    news_subcommands = news.add_subparsers(dest="news_command", required=True)
    news_subcommands.add_parser(
        "bus-check", help="connect to RabbitMQ, declare the News topology, and print queue depths"
    )
    news_control = news_subcommands.add_parser("control", help="write a delivery control command to news_control_state")
    news_control.add_argument(
        "action",
        choices=("pause_delivery", "resume_delivery", "mute_theme", "mute_symbol", "unmute"),
    )
    news_control.add_argument("--key", default="", help="theme name or symbol for mute/unmute")
    news_control.add_argument("--ttl-minutes", type=_positive_int, default=360, help="mute duration")
    news_label = news_subcommands.add_parser("label", help="record an operator label for one Event (learning plane)")
    news_label.add_argument("event_id")
    news_label.add_argument("label", choices=("good", "noise", "late", "wrong_direction", "dup"))
    news_label.add_argument("--note", default="", help="free-text note (<=200 chars)")
    news_eval = news_subcommands.add_parser("eval", help="offline evaluation of Triage decisions against labels")
    news_eval.add_argument("--hours", type=_positive_int, default=168, help="look-back window")
    news_eval.add_argument("--policy-version", default="", help="restrict to one triage policy version")
    news_replay_decisions = news_subcommands.add_parser(
        "replay-decisions", help="re-run decide() over stored verdicts with a candidate policy (no model)"
    )
    news_replay_decisions.add_argument("--hours", type=_positive_int, default=168, help="look-back window")
    news_replay_decisions.add_argument("--escalate-magnitude", type=_positive_int, default=3)
    news_replay_decisions.add_argument("--min-push-magnitude", type=_positive_int, default=2)
    news_replay_decisions.add_argument("--min-watchlist-magnitude", type=_positive_int, default=1)
    news_replay = news_subcommands.add_parser(
        "replay", help="replay a JSON file of provider hits through Deduper+Gate (no model, no broker)"
    )
    news_replay.add_argument("path", help="JSON file: {strategy_id: [hit, ...]} or [hit, ...]")
    news_dlq = news_subcommands.add_parser("dlq", help="inspect, replay, or purge the News dead-letter queue")
    news_dlq.add_argument("dlq_action", choices=("inspect", "replay", "purge"))
    news_dlq.add_argument("--limit", type=_positive_int, default=20, help="messages to inspect/replay")

    ops = subcommands.add_parser("ops", help="maintenance commands")
    ops_subcommands = ops.add_subparsers(dest="ops_command", required=True)
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
    validate_projections = ops_subcommands.add_parser(
        "validate-projections",
        help="validate projection read models against PostgreSQL facts",
    )
    validate_projections.add_argument("--sample", type=_nonnegative_int, default=100)
    return parser
