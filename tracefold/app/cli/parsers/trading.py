from __future__ import annotations

import argparse

from tracefold.app.cli.parsers.common import _positive_int


def add_trading_commands(
    subcommands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    trading = subcommands.add_parser("trading", help="inspect Trading facts and record bounded operator intent")
    commands = trading.add_subparsers(dest="trading_command", required=True)
    commands.add_parser("status", help="show Alpha producer and disabled execution readiness")

    cases = commands.add_parser("cases", help="list Trading cases newest first")
    cases.add_argument(
        "--state",
        choices=("PENDING", "RUNNING", "NO_TRADE", "SIGNAL_EMITTED", "BLOCKED"),
        default=None,
    )
    cases.add_argument("--limit", type=_positive_int, default=20)

    signals = commands.add_parser("signals", help="list engine-neutral TradeSignalV1 rows")
    signals.add_argument("--limit", type=_positive_int, default=20)

    observations = commands.add_parser("observations", help="list append-only Runtime observations")
    observations.add_argument("--limit", type=_positive_int, default=20)

    operator_intents = commands.add_parser("commands", help="list authenticated OperatorIntentV1 rows")
    operator_intents.add_argument(
        "--action",
        choices=("pause_entries", "resume_entries", "emergency_halt", "flatten", "manual_entry"),
        default=None,
    )
    operator_intents.add_argument("--limit", type=_positive_int, default=20)

    issue = commands.add_parser("issue", help="durably record one local OS-authenticated operator intent")
    issue.add_argument("text", help="closed slash command, for example '/pause maintenance'")
    issue.add_argument(
        "--request-id",
        required=True,
        help="stable caller identity; preserve it together with --requested-at-ns on retries",
    )
    issue.add_argument(
        "--requested-at-ns",
        required=True,
        type=_positive_int,
        help="caller-sealed Unix nanosecond clock; preserve it on retries",
    )

    # #459 Stage A: offline research, two commands rather than one. Collection is a fact about a
    # 30-day Binance window that cannot be served again; evaluation is a claim about a rule. Folding
    # them together would make every re-scoring re-collect, and no receipt could name its own data.
    corpus = commands.add_parser(
        "oi-corpus", help="pull or re-seal the sealed Binance open-interest research corpus (#459)"
    )
    corpus.add_argument("corpus_action", choices=("pull", "seal"))
    corpus.add_argument("--out", default=None, help="corpus directory (default: a dated one under ~/.tracefold)")
    corpus.add_argument("--days", type=_positive_int, default=29, help="window length; Binance keeps 30")
    corpus.add_argument("--symbols", nargs="*", default=None, help="restrict the universe, for a smoke pull")
    corpus.add_argument("--concurrency", type=_positive_int, default=8)

    replay = commands.add_parser(
        "oi-replay", help="score the pre-registered #459 rule over a sealed corpus; writes a receipt"
    )
    replay.add_argument("--corpus", default=None, help="corpus directory (default: a dated one under ~/.tracefold)")
    replay.add_argument("--out", default=None, help="receipt path (default: <corpus>/replay_receipt.json)")
    replay.add_argument("--trials", type=_positive_int, default=2_000, help="permutation draws")


__all__ = ["add_trading_commands"]
