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

    news = subcommands.add_parser("news", help="News V3 broker, ReviewDesk, and learning commands")
    news_subcommands = news.add_subparsers(dest="news_command", required=True)
    news_subcommands.add_parser(
        "bus-check", help="connect to RabbitMQ, declare the News topology, and print queue depths"
    )
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
    review_queue.add_argument("--view", choices=("queue", "coverage", "proposals", "market"), default="queue")
    review_queue.add_argument("--mode", choices=("event", "pairwise"), default="event")
    review_queue.add_argument("--cohort", default="")
    review_queue.add_argument("--stratum", default="")
    review_queue.add_argument("--proposal", default="")
    review_queue.add_argument("--task", default="")
    review_queue.add_argument("--event", default="")
    review_queue.add_argument("--status", choices=("pending", "accepted", "all"), default="pending")
    review_queue.add_argument("--hours", type=_positive_int, default=24)
    review_queue.add_argument("--limit", type=_positive_int, default=30)
    review_queue.add_argument("--cursor", default="")
    review_evidence = review_subcommands.add_parser("evidence", help="show the task-scoped evidence view")
    review_evidence.add_argument("task")
    review_evidence.add_argument("--version", required=True)
    review_submit = review_subcommands.add_parser("submit", help="append and accept one rubric or pairwise judgment")
    review_submit.add_argument("task")
    review_submit.add_argument("--version", required=True)
    review_submit.add_argument("--file", required=True)
    review_submit.add_argument("--idempotency-key", default="")
    review_external = review_subcommands.add_parser("external-miss", help="append an external miss and its rubric")
    review_external.add_argument("--file", required=True)
    review_external.add_argument("--idempotency-key", default="")
    news_learning = news_subcommands.add_parser(
        "learning", help="freeze reviewed datasets and evaluate one-variable Agent candidates"
    )
    learning_subcommands = news_learning.add_subparsers(dest="learning_command", required=True)
    learning_compile = learning_subcommands.add_parser(
        "compile", help="compile a bounded DSPy Program candidate from accepted development evidence"
    )
    learning_compile.add_argument("--development", required=True, help="development dataset artifact SHA")
    learning_compile.add_argument(
        "--artifact-root", required=True, help="write <program-sha>/manifest.json and state.json"
    )
    learning_compile.add_argument("--out", required=True, help="write compile receipt and proposal input JSON")
    learning_compile.add_argument(
        "--compiler-image",
        required=True,
        help="exact local compiler image ID (sha256:<64 hex>)",
    )
    learning_compile.add_argument("--max-metric-calls", type=_positive_int, required=True)
    learning_compile.add_argument("--max-task-model-calls", type=_positive_int, required=True)
    learning_compile.add_argument("--max-cost-microusd", type=_positive_int, required=True)
    learning_compile.add_argument("--seed", type=_nonnegative_int, default=129)
    learning_baseline = learning_subcommands.add_parser(
        "baseline", help="score the stable Program over accepted reviews (no sandbox, no tariff, no writes)"
    )
    learning_baseline.add_argument("--from-ms", type=_nonnegative_int, required=True)
    learning_baseline.add_argument("--to-ms", type=_positive_int, required=True)
    # `replay` (re-run the graph over a recorded Predictor corpus) is deliberately absent: the harness supports
    # the mode, but nothing here builds the recording corpus yet, so exposing it would run a live provider
    # while the flag said otherwise. A mode that quietly does something else is worse than a missing one.
    learning_baseline.add_argument(
        "--mode",
        choices=("recorded", "live"),
        default="recorded",
        help="recorded: score the persisted verdict, no model call; live: re-run the Program against the provider",
    )
    learning_baseline.add_argument(
        "--action-source",
        choices=("recorded", "policy"),
        default="",
        help="recorded: the action that shipped; policy: re-run decide(). Defaults to recorded for --mode recorded",
    )
    learning_baseline.add_argument(
        "--all-cohorts",
        action="store_true",
        help="drop release-plane eligibility and score every accepted review in the window",
    )
    learning_baseline.add_argument("--limit", type=_positive_int, default=500)
    learning_baseline.add_argument("--out", default="", help="write the baseline report JSON")
    learning_propose = learning_subcommands.add_parser("propose", help="seal a Program or policy candidate manifest")
    learning_propose.add_argument("--development", required=True, help="development dataset artifact SHA")
    learning_propose.add_argument("--file", required=True, help="candidate proposal JSON/YAML")
    learning_propose.add_argument("--out", required=True, help="write the sealed candidate manifest")
    learning_freeze = learning_subcommands.add_parser("freeze", help="freeze accepted reviews into a dataset")
    learning_freeze.add_argument("--role", choices=("development", "validation"), required=True)
    learning_freeze.add_argument("--from-ms", type=_nonnegative_int, required=True)
    learning_freeze.add_argument("--to-ms", type=_positive_int, required=True)
    learning_freeze.add_argument("--candidate", default="", help="candidate manifest; required for validation")
    learning_freeze.add_argument("--out", required=True, help="write the dataset manifest")
    for action, stage in (("evaluate", None), ("shadow", "shadow")):
        learning_eval = learning_subcommands.add_parser(action, help=f"run the {action} release-evidence gate")
        learning_eval.add_argument("--development", required=True, help="development dataset artifact SHA")
        learning_eval.add_argument("--validation", default="", help="validation dataset SHA")
        learning_eval.add_argument("--candidate", required=True, help="candidate manifest JSON/YAML")
        execution_mode = learning_eval.add_mutually_exclusive_group()
        if stage is None:
            learning_eval.add_argument(
                "--stage",
                choices=("offline", "holdout", "canary"),
                default="offline",
                help="evaluation evidence stage",
            )
            execution_mode.add_argument(
                "--live-program",
                action="store_true",
                help="run the assigned DSPy Program live and append per-Predictor recordings",
            )
            execution_mode.add_argument(
                "--verify-recordings",
                action="store_true",
                help="strictly re-run an existing offline/holdout corpus without live provider calls",
            )
            learning_eval.add_argument(
                "--observation-manifest",
                default="",
                help="optional sealed canary observation artifact SHA",
            )
        else:
            learning_eval.add_argument(
                "--observation-manifest",
                default="",
                help="reuse a sealed shadow observation artifact instead of collecting one",
            )
            execution_mode.add_argument(
                "--live-program",
                action="store_true",
                help="cold-run the candidate Program over the closed validation window",
            )
        learning_eval.add_argument("--out", required=True, help="write the sealed evaluation report")
    learning_canary = learning_subcommands.add_parser(
        "canary", help="arm, inspect, or stop the durable one-arm production canary"
    )
    canary_subcommands = learning_canary.add_subparsers(dest="canary_command", required=True)
    canary_arm = canary_subcommands.add_parser("arm", help="arm the image-carried candidate at code-owned exposure")
    canary_arm.add_argument("--candidate", required=True, help="sealed CandidateManifest SHA carried by this image")
    canary_subcommands.add_parser("status", help="show activation, revision, and assignment counts")
    for transition in ("hold", "resume", "trip", "close"):
        canary_transition = canary_subcommands.add_parser(transition, help=f"{transition} one activation")
        canary_transition.add_argument("--activation", required=True)
        canary_transition.add_argument("--reason", required=True)
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
    news_replay.add_argument(
        "--no-instruments",
        action="store_true",
        help="replay without the instrument universe (offline); the Gate then guesses asset_class from XYZ- tags",
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
