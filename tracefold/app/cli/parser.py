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
    nautilus = subcommands.add_parser("nautilus", help="run the Production V3 execution authority")
    nautilus_subcommands = nautilus.add_subparsers(dest="nautilus_command", required=True)
    nautilus_run = nautilus_subcommands.add_parser("run", help="run the single Nautilus TradingNode process")
    nautilus_run.add_argument(
        "--bootstrap-zero-claims",
        action="store_true",
        help="prove a paused bound account is empty before activating execution truth",
    )

    init = subcommands.add_parser("init", help="create ~/.tracefold/config.yaml")
    init.add_argument("--force", action="store_true", help="overwrite existing config.yaml")

    subcommands.add_parser("config", help="print effective runtime configuration")

    db = subcommands.add_parser("db", help="database lifecycle commands")
    db_subcommands = db.add_subparsers(dest="db_command", required=True)
    db_subcommands.add_parser("migrate", help="apply PostgreSQL migrations")
    db_subcommands.add_parser("health", help="check PostgreSQL liveness and migration version")
    db_audit = db_subcommands.add_parser("audit", help="run the fast PostgreSQL schema/role/catalog audit")
    db_audit.add_argument("--deep", action="store_true", help="also run offline exact counts over every table")
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
    review_accept = review_subcommands.add_parser(
        "accept-drafts",
        help="submit reviewed model drafts through the normal submit path (operator remains the author)",
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
        help="comma-separated event_id or task_id prefixes to accept; default is every draft that passes the filters",
    )
    review_accept.add_argument(
        "--exclude",
        default="",
        help="comma-separated event_id or task_id prefixes to skip after you have read them",
    )
    review_accept.add_argument(
        "--reviewer",
        default="model_drafted_operator",
        help=(
            "reviewer recorded on each row. The default marks these as operator-accepted model drafts so they "
            "stay identifiable — and excludable — if their label noise later proves to matter"
        ),
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
    # #199 P0. The one formal answer to "what would GEPA actually optimize on this corpus, and why is
    # everything else out" — read-only, and it makes no task, reflection or judge call at all. It is an
    # explanation in advance, not a bypass: `optimize` rebuilds the same Objective Plan and refuses on
    # the same conditions, so a blocked corpus is answered for free instead of for a model budget.
    learning_readiness = learning_subcommands.add_parser(
        "readiness",
        help="explain the Objective Plan for a frozen development dataset; 0 model calls, 0 writes",
    )
    learning_readiness.add_argument("--development", required=True, help="development dataset artifact SHA")
    learning_readiness.add_argument(
        "--out", default="", help="write the readiness report JSON (per-case dispositions live only here)"
    )
    learning_baseline = learning_subcommands.add_parser(
        "baseline", help="score the stable Program over accepted reviews (no sandbox, no tariff, no writes)"
    )
    # Two corpora, one command, and the receipt says which. `--from-ms/--to-ms` is a moving window and is
    # discovery: the population changes with the clock, so a before/after taken across two of them compares
    # two different corpora. `--dataset` is the exact frozen development dataset `optimize` reads, scored
    # under the same Objective Plan and publishing the same split roots — that one is release evidence.
    # They are mutually exclusive because a run can only be one of the two (#199 §5).
    learning_baseline.add_argument("--from-ms", type=_nonnegative_int, default=None)
    learning_baseline.add_argument("--to-ms", type=_positive_int, default=None)
    learning_baseline.add_argument(
        "--dataset",
        default="",
        metavar="SHA",
        help=(
            "score the exact frozen development dataset instead of a moving window; "
            "mutually exclusive with --from-ms/--to-ms"
        ),
    )
    # `live` is gone, not aliased. It answered two different questions under one name: the graph GEPA
    # optimizes, and the production route's reliability. Keeping an alias would keep the ambiguity alive.
    learning_baseline.add_argument(
        "--mode",
        choices=("recorded", "compile_live", "runtime_live"),
        default="recorded",
        help=(
            "recorded: score the persisted verdict against the action that shipped, no model call; "
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
            "(e.g. deepseek-v4-pro). Enum dimensions stay exact. Costs nothing under --mode recorded, "
            "where the candidate is the production verdict and the texts already match"
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
    learning_draft.add_argument("--model", required=True, help="drafting model, e.g. deepseek-v4-pro")
    learning_draft.add_argument("--limit", type=_positive_int, default=50)
    learning_draft.add_argument(
        "--include-reviewed",
        action="store_true",
        help="also draft Events that already carry an accepted review (default: only unjudged ones)",
    )
    learning_draft.add_argument("--out", required=True, help="write the draft batch JSON for human review")
    learning_subcommands.add_parser(
        "taxonomy-register",
        help="register one frozen taxonomy shadow candidate before opening its future holdout",
    )
    learning_taxonomy_shadow = learning_subcommands.add_parser(
        "taxonomy-shadow",
        help="run bounded taxonomy shadow cases and append terminal observations",
    )
    learning_taxonomy_shadow.add_argument("--file", required=True, help="JSON/YAML mapping with a cases array")
    learning_taxonomy_shadow.add_argument("--limit", type=_positive_int, default=50)
    learning_taxonomy_shadow.add_argument("--out", required=True, help="write the shadow artifact receipt JSON")
    learning_taxonomy = learning_subcommands.add_parser(
        "taxonomy-evaluate",
        help="seal a news_taxonomy_v1 evaluation over frozen Gold/shadow cases",
    )
    learning_taxonomy.add_argument("--file", required=True, help="JSON/YAML mapping with a cases array")
    learning_taxonomy.add_argument("--out", required=True, help="write TaxonomyEvaluationReportV1 JSON")
    # #202. The one optimization entry point. It replaces `compile` (a sealed container against a metered
    # proxy) and `experiment optimize` (the same algorithm in process, behind `promotable=false`), which
    # produced two candidate lifecycles for one two-string write-set. It holds no database write, broker,
    # delivery, canary or promotion authority, and it ends in NO_OP, REJECTED or ADVANCE.
    learning_optimize = learning_subcommands.add_parser(
        "optimize",
        help="run the one bounded GEPA optimization over a frozen development dataset; ADVANCE is not a release",
    )
    learning_optimize.add_argument("--development", required=True, help="development dataset artifact SHA")
    learning_optimize.add_argument("--out", required=True, help="directory for the run report and any candidate")
    learning_optimize.add_argument("--max-metric-calls", type=_positive_int, required=True)
    learning_optimize.add_argument("--max-task-model-calls", type=_positive_int, required=True)
    learning_optimize.add_argument("--max-reflection-model-calls", type=_positive_int, required=True)
    learning_optimize.add_argument("--max-metric-judge-model-calls", type=_positive_int, required=True)
    learning_optimize.add_argument("--max-cost-microusd", type=_positive_int, required=True)
    # The per-call ceiling is also the rate an unpriced call is charged at: neither endpoint this project
    # runs on reports a resolvable price, and the proxy tariff that used to answer that is gone with the
    # proxy. Over-charging stops a run early rather than late.
    learning_optimize.add_argument("--max-call-cost-microusd", type=_positive_int, required=True)
    learning_optimize.add_argument(
        "--max-wall-clock-seconds", type=_positive_int, default=14_400, help="deadline checked before each call"
    )
    learning_optimize.add_argument("--seed", type=_nonnegative_int, default=129)
    # #253 §7 Phase C. The one recommended path: readiness, the standalone `compile_live` baseline over the
    # same frozen corpus, and the one optimization, composed in one process over one dataset SHA. It owns
    # no Objective Plan, Metric, split, budget or optimizer of its own, which is why it carries their
    # budget flags rather than defaults of its own. It takes no `--semantic-judge`: the judge is the
    # configured compiler reflection route, so the baseline and GEPA cannot be handed two different rulers.
    learning_run = learning_subcommands.add_parser(
        "run",
        help="the recommended path: readiness -> standalone baseline -> optimize, into one directory",
    )
    learning_run.add_argument("--development", required=True, help="development dataset artifact SHA")
    learning_run.add_argument("--out", required=True, help="run directory for every artifact this run writes")
    learning_run.add_argument(
        "--max-baseline-model-cases",
        type=_positive_int,
        required=True,
        help=(
            "bound on cases the standalone baseline may send to a provider; it must cover the whole "
            "optimizer corpus, and readiness checks that before the first call"
        ),
    )
    learning_run.add_argument("--max-metric-calls", type=_positive_int, required=True)
    learning_run.add_argument("--max-task-model-calls", type=_positive_int, required=True)
    learning_run.add_argument("--max-reflection-model-calls", type=_positive_int, required=True)
    learning_run.add_argument(
        "--max-metric-judge-model-calls",
        type=_positive_int,
        required=True,
        help=(
            "judge call ceiling for the optimization only; the standalone baseline's judge takes no "
            "ceiling (reaching one scores cases zero rather than raising) and is bounded by "
            "--max-baseline-model-cases"
        ),
    )
    learning_run.add_argument("--max-cost-microusd", type=_positive_int, required=True)
    learning_run.add_argument("--max-call-cost-microusd", type=_positive_int, required=True)
    learning_run.add_argument("--max-wall-clock-seconds", type=_positive_int, default=14_400)
    learning_run.add_argument("--seed", type=_nonnegative_int, default=129)
    # #202 §11 PR-E. Two command groups, because there are two lifecycles. `news learning` freezes a
    # corpus, explains what GEPA may optimize, scores the stable Program and runs the one optimization —
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
    learning_register.add_argument("--candidate", required=True, help="news_prompt_candidate_v1 JSON/YAML")
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

    trading = subcommands.add_parser("trading", help="Trading Case -> Intent -> Outcome and safety controls")
    trading_subcommands = trading.add_subparsers(dest="trading_command", required=True)
    trading_subcommands.add_parser("status", help="Decision, Capital, binding facts, and durable outcomes")
    trading_cases = trading_subcommands.add_parser("cases", help="list Trading cases newest first")
    trading_cases.add_argument(
        "--state",
        choices=("PENDING", "RUNNING", "NO_TRADE", "POLICY_REJECTED", "INTENT_EMITTED", "BLOCKED"),
        default=None,
    )
    trading_cases.add_argument("--limit", type=_positive_int, default=20)
    trading_replay = trading_subcommands.add_parser(
        "replay-oi",
        help="source-native BAR replay with an audited artifact and immutable receipt (#286)",
    )
    trading_replay.add_argument(
        "--days",
        type=_positive_int,
        default=7,
        help="how far back to replay; the OI ledger holds 30 days of parsed frames",
    )
    trading_replay.add_argument(
        "--strategy",
        choices=("source_native_oi_smart_money_long_v3",),
        default="source_native_oi_smart_money_long_v3",
        help="the one production capital policy; a replay may only run the identity the lane runs",
    )
    trading_replay.add_argument(
        "--venues",
        default="binance.perp,hl.perp",
        help="comma-separated exact source-native venue scenarios",
    )
    trading_replay.add_argument("--fidelity", choices=("bar_v1",), default="bar_v1")
    trading_replay.add_argument("--out", default="artifacts/trading-replay", help="artifact root")
    trading_evidence = trading_subcommands.add_parser(
        "evidence",
        help="capture, seal, preregister, unblind, and verify the Production V3 evidence clock",
    )
    evidence_subcommands = trading_evidence.add_subparsers(dest="evidence_command", required=True)
    evidence_capture = evidence_subcommands.add_parser(
        "capture", help="freeze a point-in-time discovery or protocol-locked future source partition"
    )
    evidence_capture.add_argument("--partition", choices=("discovery", "future"), required=True)
    evidence_capture.add_argument("--start-ms", type=_nonnegative_int, required=True)
    evidence_capture.add_argument("--end-ms", type=_positive_int, required=True)
    evidence_capture.add_argument("--candidate", default="")
    evidence_capture.add_argument("--candidate-receipt", default="")
    evidence_capture.add_argument("--out", required=True, help="content-addressed evidence artifact root")
    evidence_drain = evidence_subcommands.add_parser(
        "drain", help="freeze bars and funding only after the capture horizon can be finalized"
    )
    evidence_drain.add_argument("--capture", required=True)
    evidence_drain.add_argument("--candidate", default="")
    evidence_drain.add_argument("--candidate-receipt", default="")
    evidence_drain.add_argument("--max-horizon-ms", type=_positive_int, default=None)
    evidence_drain.add_argument("--finalization-lag-ms", type=_nonnegative_int, default=None)
    evidence_drain.add_argument("--cost-model", default="")
    evidence_drain.add_argument("--out", required=True, help="content-addressed evidence artifact root")
    evidence_seal = evidence_subcommands.add_parser(
        "corpus-seal", help="deterministically seal a discovery capture and drain; zero provider I/O"
    )
    evidence_seal.add_argument("--capture", required=True)
    evidence_seal.add_argument("--drain", required=True)
    evidence_seal.add_argument("--out", required=True, help="content-addressed evidence artifact root")
    evidence_register = evidence_subcommands.add_parser(
        "candidate-register", help="durably register one candidate or NO_CANDIDATE before a future window"
    )
    evidence_register.add_argument("--file", required=True)
    evidence_register.add_argument("--out", required=True, help="content-addressed evidence artifact root")
    evidence_release_register = evidence_subcommands.add_parser(
        "release-register",
        help="bind an approved exact release and fixed window to the current Workers/Serve generations",
    )
    evidence_release_register.add_argument("--file", required=True, help="approved release candidate YAML/JSON")
    evidence_unblind = evidence_subcommands.add_parser(
        "future-unblind", help="evaluate one protocol-locked future partition after its fixed drain cutoff"
    )
    evidence_unblind.add_argument("--capture", required=True)
    evidence_unblind.add_argument("--drain", required=True)
    evidence_unblind.add_argument("--candidate", required=True)
    evidence_unblind.add_argument("--candidate-receipt", required=True)
    evidence_unblind.add_argument("--out", required=True, help="content-addressed evidence artifact root")
    evidence_verify = evidence_subcommands.add_parser(
        "verify", help="credential-free verification of one evidence chain, lifecycle, release, window, or rollback"
    )
    verification_subject = evidence_verify.add_mutually_exclusive_group(required=True)
    verification_subject.add_argument("--receipt", default="", help="durable evidence receipt SHA")
    verification_subject.add_argument("--case-id", default="", help="single durable Case/Intent lifecycle")
    verification_subject.add_argument("--window", default="", help="fixed seven-day acceptance YAML/JSON")
    verification_subject.add_argument("--release", default="", help="exact approved release candidate YAML/JSON")
    verification_subject.add_argument("--rollback", default="", help="rollback receipt YAML/JSON")
    trading_show = trading_subcommands.add_parser("show", help="one case with its intent and current outcome")
    trading_show.add_argument("case_id")
    trading_blacklist = trading_subcommands.add_parser(
        "blacklist",
        help="the canonical deny-list; one row blocks every provider spelling of that underlying",
    )
    trading_blacklist.add_argument("blacklist_action", choices=("list", "add", "remove"))
    trading_blacklist.add_argument("symbol", nargs="?", default="")
    trading_blacklist.add_argument("--reason", default="operator")
    trading_control = trading_subcommands.add_parser("control", help="set the runtime control state")
    trading_control.add_argument("state", choices=("close-only", "paused"))
    trading_authority = trading_subcommands.add_parser(
        "authority", help="install immutable human authority artifacts and explicitly re-arm"
    )
    authority_subcommands = trading_authority.add_subparsers(dest="authority_command", required=True)
    for action, help_text in (
        ("risk-policy-install", "install one DailyRiskPolicyV1 JSON/YAML artifact"),
        ("grant-install", "install one ProductionPromotionGrantV1 JSON/YAML artifact"),
        ("grant-revoke", "append one ProductionPromotionGrantRevocationV1 JSON/YAML artifact"),
        ("arm-install", "install one OperatorArmReceiptV1 JSON/YAML artifact while paused"),
    ):
        authority_file = authority_subcommands.add_parser(action, help=help_text)
        authority_file.add_argument("--file", required=True)
    authority_activate = authority_subcommands.add_parser(
        "activate", help="atomically activate the exact arm set for every configured binding"
    )
    authority_activate.add_argument(
        "--arm", action="append", required=True, help="arm receipt SHA; repeat once per configured binding"
    )

    ops = subcommands.add_parser("ops", help="maintenance commands")
    ops_subcommands = ops.add_subparsers(dest="ops_command", required=True)
    validate_projections = ops_subcommands.add_parser(
        "validate-projections",
        help="validate projection read models against PostgreSQL facts",
    )
    validate_projections.add_argument("--sample", type=_nonnegative_int, default=100)
    return parser
