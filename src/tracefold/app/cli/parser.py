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
    subcommands.add_parser("workers", help="run the News ingestion, triage, and delivery runtime")

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
    news_instruments = news_subcommands.add_parser(
        "instruments", help="tradeable instrument universe: snapshot the venues, or inspect what is stored"
    )
    news_instruments.add_argument(
        "action", choices=("snapshot", "summary", "listings", "resolve"), nargs="?", default="summary"
    )
    news_instruments.add_argument("--symbol", default="", help="symbol to resolve (action=resolve)")
    news_instruments.add_argument("--hours", type=_positive_int, default=168, help="look-back (action=listings)")
    news_instruments.add_argument("--limit", type=_positive_int, default=50, help="max rows (action=listings)")
    news_label = news_subcommands.add_parser("label", help="record an operator label for one Event (learning plane)")
    news_label.add_argument("event_id")
    news_label.add_argument("label", choices=("good", "noise", "late", "wrong_direction", "dup", "missed"))
    news_label.add_argument("--note", default="", help="free-text note (<=200 chars)")
    news_eval = news_subcommands.add_parser("eval", help="offline evaluation of Triage decisions against labels")
    news_eval.add_argument("--hours", type=_positive_int, default=168, help="look-back window")
    news_eval.add_argument("--policy-version", default="", help="restrict to one triage policy version")
    news_replay_decisions = news_subcommands.add_parser(
        "replay-decisions", help="re-run decide() over stored verdicts with a candidate policy (no model)"
    )
    news_replay_decisions.add_argument("--hours", type=_positive_int, default=168, help="look-back window")
    news_replay_decisions.add_argument("--escalate-magnitude", type=int, default=None, help="default: news.policy")
    news_replay_decisions.add_argument("--min-push-magnitude", type=int, default=None, help="default: news.policy")
    news_replay_decisions.add_argument("--min-watchlist-magnitude", type=int, default=None, help="default: news.policy")
    news_replay_decisions.add_argument("--theme-cap-4h", type=_positive_int, default=None, help="default: news.policy")
    news_replay_decisions.add_argument(
        "--theme-hard-cap-4h",
        type=_positive_int,
        default=None,
        help="novel-event hard cap per theme / 4 h; default: news.policy",
    )
    news_replay_decisions.add_argument(
        "--asset-hard-cap-2h",
        type=_positive_int,
        default=None,
        help="novel-event hard cap per asset / 2 h; default: news.policy",
    )
    news_replay_decisions.add_argument(
        "--novel-min-magnitude",
        type=int,
        default=None,
        help="minimum magnitude for the novelty bypass; default: news.policy",
    )
    news_replay_decisions.add_argument(
        "--no-restatement-drop", action="store_true", help="replay without dropping grounded restatements"
    )
    news_replay_decisions.add_argument(
        "--no-storyline-throttle", action="store_true", help="replay with storyline throttling switched off"
    )
    news_replay_decisions.add_argument(
        "--no-unclear-push", action="store_true", help="replay without the unclear-but-clear-event push rule"
    )
    news_replay = news_subcommands.add_parser(
        "replay", help="replay a JSON file of provider hits through Deduper+Gate (no model, no broker)"
    )
    news_replay.add_argument("path", help="JSON file: {strategy_id: [hit, ...]} or [hit, ...]")
    news_replay.add_argument(
        "--gate-policy",
        choices=("config", "open", "strict"),
        default="config",
        help="Gate low-signal switch: config = news.gate.suppress_low_signal, open = off, strict = on",
    )
    news_why = news_subcommands.add_parser("why", help="print one Event's chain: item, gate, triage, decide, delivery")
    news_why.add_argument("event_id")
    news_dlq = news_subcommands.add_parser("dlq", help="inspect, replay, or purge the News dead-letter queue")
    news_dlq.add_argument("dlq_action", choices=("inspect", "replay", "purge"))
    news_dlq.add_argument("--limit", type=_positive_int, default=20, help="messages to inspect/replay")

    ops = subcommands.add_parser("ops", help="maintenance commands")
    ops_subcommands = ops.add_subparsers(dest="ops_command", required=True)
    validate_projections = ops_subcommands.add_parser(
        "validate-projections",
        help="validate projection read models against PostgreSQL facts",
    )
    validate_projections.add_argument("--sample", type=_nonnegative_int, default=100)
    return parser
