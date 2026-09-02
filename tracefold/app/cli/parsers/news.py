from __future__ import annotations

import argparse

from tracefold.app.cli.parsers.common import _nonnegative_int, _positive_int


def add_news_commands(
    subcommands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    news = subcommands.add_parser("news", help="News V3 broker, ReviewDesk, and learning commands")
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
    review_evidence.add_argument(
        "--source-only",
        action="store_true",
        help="show only the pinned TaskRef and source evidence, excluding Stable, drafts, and reviews",
    )
    review_submit = review_subcommands.add_parser("submit", help="append and accept one rubric or pairwise judgment")
    review_submit.add_argument("task")
    review_submit.add_argument("--version", required=True)
    review_submit.add_argument("--file", required=True)
    review_submit.add_argument("--reviewer", required=True, help="actual reviewer principal persisted on the review")
    review_submit.add_argument("--idempotency-key", default="")
    review_accept = review_subcommands.add_parser(
        "accept-drafts",
        help="submit reviewed model drafts through ReviewDesk under the named reviewer's identity",
    )
    review_accept.add_argument("--file", required=True, help="draft batch produced by `learning draft-reviews`")
    review_accept.add_argument(
        "--min-confidence",
        type=float,
        default=0.0,
        help="skip drafts the model was less sure of than this (0.0-1.0)",
    )
    review_accept.add_argument(
        "--only",
        default="",
        help=(
            "required for writes: comma-separated event_id or task_id prefixes explicitly approved; "
            "an empty value is allowed only with --dry-run"
        ),
    )
    review_accept.add_argument(
        "--exclude",
        default="",
        help="comma-separated event_id or task_id prefixes to skip after you have read them",
    )
    review_accept.add_argument(
        "--reviewer",
        default="",
        help=(
            "required for writes: actual accepting reviewer recorded on each row, including an identified AI "
            "adjudicator; an empty value is allowed only with --dry-run"
        ),
    )
    review_accept.add_argument(
        "--first-bad-owner",
        default="",
        help="explicit owner written into every selected review, for example taxonomy; omitted keeps null",
    )
    review_accept.add_argument(
        "--dry-run",
        action="store_true",
        help="report exactly what would be submitted, and write nothing",
    )
    review_external = review_subcommands.add_parser("external-miss", help="append an external miss and its rubric")
    review_external.add_argument("--file", required=True)
    review_external.add_argument("--idempotency-key", default="")
    news_learning = news_subcommands.add_parser(
        "learning", help="freeze reviewed datasets and evaluate one-variable Agent candidates"
    )
    learning_subcommands = news_learning.add_subparsers(dest="learning_command", required=True)
    # #199 P0. The formal zero-call answer to what the one `run` command would optimize.
    learning_readiness = learning_subcommands.add_parser(
        "readiness",
        help="explain the Objective Plan for a frozen development dataset; 0 model calls, 0 writes",
    )
    learning_readiness.add_argument("--development", required=True, help="development dataset artifact SHA")
    learning_readiness.add_argument(
        "--out", default="", help="write the readiness report JSON (per-case dispositions live only here)"
    )
    learning_baseline = learning_subcommands.add_parser(
        "baseline",
        help="score a moving-window Program baseline (no sandbox, tariff, or writes)",
    )
    # Baseline is discovery over a moving population. The frozen corpus belongs only to `readiness` and
    # `run`, so there is no second candidate-generating or dataset-baseline route.
    learning_baseline.add_argument("--from-ms", type=_nonnegative_int, required=True)
    learning_baseline.add_argument("--to-ms", type=_positive_int, required=True)
    # `live` is gone, not aliased. It answered two different questions under one name: the graph GEPA
    # optimizes, and the production route's reliability. Keeping an alias would keep the ambiguity alive.
    learning_baseline.add_argument(
        "--mode",
        choices=("recorded", "compile_live", "runtime_live"),
        default="recorded",
        help=(
            "recorded: no model call; score persisted action for the moving window; "
            "compile_live: the graph GEPA optimizes, one task endpoint, no route fallback/deadline/circuit; "
            "per-call timeout and JSON format fallback remain; "
            "runtime_live: the configured four-slot production Program route (excludes consumer transaction, "
            "advisory lock, stale re-ask, degraded wire card, broker and delivery)"
        ),
    )
    learning_baseline.add_argument(
        "--action-source",
        choices=("recorded", "policy"),
        default="",
        help=(
            "recorded: the action that shipped, valid only with --mode recorded; policy: re-run decide(), "
            "required by the live modes. Defaults to the only valid value for the chosen mode"
        ),
    )
    learning_baseline.add_argument(
        "--max-model-cases",
        type=int,
        default=0,
        help=(
            "required by --mode compile_live and runtime_live: the most cases allowed to reach a provider. "
            "runtime_live spends 2-8 real calls per case, sequentially, on the endpoints that also serve "
            "production Triage"
        ),
    )
    learning_baseline.add_argument(
        "--semantic-judge",
        default="",
        metavar="MODEL",
        help=(
            "score free-text retention anchors by meaning instead of byte equality, using this model "
            "(e.g. deepseek-v4-pro). Enum dimensions stay exact. Moving-window recorded costs nothing "
            "because persisted texts already match"
        ),
    )
    learning_baseline.add_argument("--limit", type=_positive_int, default=500)
    learning_baseline.add_argument("--out", default="", help="write the baseline report JSON")
    learning_draft = learning_subcommands.add_parser(
        "draft-reviews",
        help="propose news_review_v6 rubrics with exact taxonomy Gold (writes a file, never the DB)",
    )
    # The ReviewDesk queue is anchored at "now" and takes a look-back width, not an absolute window, so this
    # command takes the same shape rather than pretending to accept one: `--from-ms/--to-ms` looked like an
    # absolute range and silently drafted today's Events whatever was passed.
    learning_draft.add_argument(
        "--hours", type=_positive_int, default=24, help="look back this many hours from now (max 720)"
    )
    learning_draft.add_argument(
        "--rubric-model", required=True, help="rubric drafting model, e.g. deepseek-v4-pro or qwen3.8-27b:thinking"
    )
    # #501 D8: two blind taxonomy drafters of different families, neither the Stable task model. That is an
    # operating rule recorded in the batch receipt, not a code check.
    learning_draft.add_argument(
        "--taxonomy-models",
        required=True,
        help="two comma-separated blind taxonomy drafting models, A,B; the draft takes A on disagreement",
    )
    learning_draft.add_argument("--limit", type=_positive_int, default=50)
    learning_draft.add_argument("--stratum", default="", help="restrict the existing ReviewDesk sampler stratum")
    learning_draft.add_argument(
        "--include-reviewed",
        action="store_true",
        help="also draft Events that already carry an accepted review (default: only unjudged ones)",
    )
    learning_draft.add_argument("--out", required=True, help="write the draft batch JSON for authorized review")
    # #453. The only candidate-generating entry point: zero-call readiness followed by exactly one stock
    # GEPA compile over the frozen development corpus. It owns no release authority.
    learning_run = learning_subcommands.add_parser(
        "run",
        help="the one bounded candidate path: readiness -> stock GEPA, into a new empty directory",
    )
    learning_run.add_argument("--development", required=True, help="development dataset artifact SHA")
    learning_run.add_argument("--out", required=True, help="run directory for every artifact this run writes")
    # Exactly one budget form, mirroring `dspy.GEPA` (#501 D6): DSPy's own `auto` preset or an explicit
    # metric-call count. Neither is floored or pre-checked here.
    learning_budget = learning_run.add_mutually_exclusive_group(required=True)
    learning_budget.add_argument("--auto", choices=("light", "medium", "heavy"), default=None)
    learning_budget.add_argument("--max-metric-calls", type=_positive_int, default=None)
    learning_run.add_argument("--max-task-model-calls", type=_positive_int, required=True)
    learning_run.add_argument("--max-reflection-model-calls", type=_positive_int, required=True)
    learning_run.add_argument("--max-cost-microusd", type=_positive_int, required=True)
    learning_run.add_argument("--max-call-cost-microusd", type=_positive_int, required=True)
    learning_run.add_argument("--max-wall-clock-seconds", type=_positive_int, default=14_400)
    learning_run.add_argument("--seed", type=_nonnegative_int, default=129)
    # #202 §11 PR-E. Two command groups, because there are two lifecycles. `news learning` freezes a
    # corpus, explains what GEPA may optimize, scores moving windows and runs the one optimization —
    # none of which can ship anything. `news release` admits a candidate, gathers release evidence and
    # moves the canary. An operator reading `--help` sees the boundary the packages have.
    news_release = news_subcommands.add_parser(
        "release", help="register a Prompt candidate, gather release evidence, and control the canary"
    )
    release_subcommands = news_release.add_subparsers(dest="release_command", required=True)

    # One registration, whatever wrote the two instructions (#202 §7). A GEPA candidate and a patch a
    # person wrote enter here on identical terms: the parent must be the active stable, the dataset must be
    # the frozen development corpus, the Objective Plan is re-derived here rather than trusted, and what
    # comes out is a proposal — never a promotion.
    learning_register = release_subcommands.add_parser(
        "register", help="bind a Prompt candidate to the active stable and a frozen dataset"
    )
    learning_register.add_argument("--development", required=True, help="development dataset artifact SHA")
    learning_register.add_argument("--candidate", required=True, help="news_prompt_candidate_v2 JSON/YAML")
    learning_register.add_argument(
        "--artifact-root", required=True, help="write the candidate <program-sha>.json artifact document"
    )
    learning_register.add_argument("--hypothesis", default="", help="what this candidate is expected to repair")
    learning_register.add_argument("--out", required=True, help="write the sealed candidate manifest")
    learning_freeze = learning_subcommands.add_parser("freeze", help="freeze accepted reviews into a dataset")
    learning_freeze.add_argument("--role", choices=("development", "validation"), required=True)
    learning_freeze.add_argument("--from-ms", type=_nonnegative_int, required=True)
    learning_freeze.add_argument("--to-ms", type=_positive_int, required=True)
    learning_freeze.add_argument("--candidate", default="", help="candidate manifest; required for validation")
    learning_freeze.add_argument("--out", required=True, help="write the dataset manifest")
    for action, stage in (("evaluate", None), ("shadow", "shadow")):
        learning_eval = release_subcommands.add_parser(action, help=f"run the {action} release-evidence gate")
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
                help="run the assigned Program live and append per-Predictor recordings",
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
    learning_canary = release_subcommands.add_parser(
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
        "--no-instruments",
        action="store_true",
        help="replay without the instrument universe (offline); the Gate then guesses asset_class from XYZ- tags",
    )
    news_why = news_subcommands.add_parser("why", help="print one Event's chain: item, gate, triage, decide, delivery")
    news_why.add_argument("event_id")
    news_dlq = news_subcommands.add_parser("dlq", help="inspect, replay, or purge the News dead-letter queue")
    news_dlq.add_argument("dlq_action", choices=("inspect", "replay", "purge"))
    news_dlq.add_argument("--limit", type=_positive_int, default=20, help="messages to inspect/replay")
