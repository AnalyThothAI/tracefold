from __future__ import annotations

import argparse

from tracefold.app.cli.parsers.common import _positive_int


def add_news_commands(
    subcommands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    news = subcommands.add_parser("news", help="News V3 broker, ReviewDesk, and judge calibration commands")
    news_subcommands = news.add_subparsers(dest="news_command", required=True)
    news_subcommands.add_parser(
        "bus-check",
        help="declare the News topology and report queue state, effective retry policy, and topology drift",
    )
    news_bus_policy = news_subcommands.add_parser(
        "bus-policy", help="apply or verify the checked-in RabbitMQ retry/dead-letter policy document"
    )
    news_bus_policy.add_argument("policy_action", choices=("apply", "verify"))
    news_instruments = news_subcommands.add_parser(
        "instruments", help="instrument universe: snapshot the venues, or inspect what is stored"
    )
    news_instruments.add_argument(
        "action", choices=("snapshot", "summary", "resolve", "unmatched"), nargs="?", default="summary"
    )
    news_instruments.add_argument("--symbol", default="", help="symbol to resolve (action=resolve)")
    news_instruments.add_argument("--days", type=_positive_int, default=7, help="look-back (action=unmatched)")
    news_instruments.add_argument("--limit", type=_positive_int, default=50, help="max rows (action=unmatched)")
    news_review = news_subcommands.add_parser("review", help="ReviewDesk queue, evidence, and append-only judgments")
    review_subcommands = news_review.add_subparsers(dest="review_command", required=True)
    review_queue = review_subcommands.add_parser("queue", help="open the deterministic operator review queue")
    review_queue.add_argument("--view", choices=("queue", "coverage", "market"), default="queue")
    review_queue.add_argument("--cohort", default="")
    review_queue.add_argument("--stratum", default="")
    review_queue.add_argument("--task", default="")
    review_queue.add_argument("--event", default="")
    review_queue.add_argument("--status", choices=("pending", "accepted", "all"), default="pending")
    review_queue.add_argument("--hours", type=_positive_int, default=24)
    review_queue.add_argument("--limit", type=_positive_int, default=30)
    review_queue.add_argument("--cursor", default="")
    review_evidence = review_subcommands.add_parser("evidence", help="show the task-scoped evidence view")
    review_evidence.add_argument("task")
    review_evidence.add_argument("--version", required=True)
    review_evidence.add_argument(
        "--source-only",
        action="store_true",
        help="show only the pinned TaskRef and source evidence, excluding the agent answer and reviews",
    )
    review_submit = review_subcommands.add_parser("submit", help="append and accept one event rubric judgment")
    review_submit.add_argument("task")
    review_submit.add_argument("--version", required=True)
    review_submit.add_argument("--file", required=True)
    review_submit.add_argument("--reviewer", required=True, help="actual reviewer principal persisted on the review")
    review_submit.add_argument("--idempotency-key", default="")
    review_external = review_subcommands.add_parser("external-miss", help="append an external miss and its rubric")
    review_external.add_argument("--file", required=True)
    review_external.add_argument("--idempotency-key", default="")
    news_learning = news_subcommands.add_parser(
        "learning", help="measure the News card judge against its fixed calibration corpus"
    )
    learning_subcommands = news_learning.add_subparsers(dest="learning_command", required=True)
    # #651 §7.3: the card judge's answers are a model's opinion, so there has to be a command that
    # measures whether that opinion tracks the perturbation it is supposed to catch. Fourteen synthetic
    # pairs, no database, and a receipt.
    learning_calibration = learning_subcommands.add_parser(
        "judge-calibration",
        help="score the card judge against the fixed perturbation corpus; writes a receipt, no DB",
    )
    learning_calibration.add_argument(
        "--model", required=True, metavar="MODEL", help="the judge model to measure, e.g. deepseek-v4-pro"
    )
    learning_calibration.add_argument("--out", default="", help="write the calibration receipt JSON")
    news_replay = news_subcommands.add_parser(
        "replay", help="replay a JSON file of provider hits through Deduper+Gate (no model, no broker)"
    )
    news_replay.add_argument("path", help="JSON file: {strategy_id: [hit, ...]} or [hit, ...]")
    news_replay.add_argument(
        "--no-instruments",
        action="store_true",
        help="replay without the instrument universe (offline); the Gate then guesses asset_class from XYZ- tags",
    )
    news_wallets = news_subcommands.add_parser(
        "wallets",
        help="explain the smart-money alert flow: roster, thresholds, collected flow, decisions, send queue",
    )
    news_wallets.add_argument("--hours", type=_positive_int, default=24, help="window for the flow and decision counts")
    news_wallets.add_argument(
        "--queue-limit", type=_positive_int, default=10, help="how many waiting deliveries to list"
    )
    news_why = news_subcommands.add_parser("why", help="print one Event's chain: item, gate, triage, decide, delivery")
    news_why.add_argument("event_id")
    news_dlq = news_subcommands.add_parser("dlq", help="inspect, replay, or purge the News dead-letter queue")
    news_dlq.add_argument("dlq_action", choices=("inspect", "replay", "purge"))
    news_dlq.add_argument("--limit", type=_positive_int, default=20, help="messages to inspect/replay")
