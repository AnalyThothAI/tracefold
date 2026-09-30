from __future__ import annotations

import argparse

from tracefold.app.cli.parsers.common import _positive_int


def add_trading_commands(
    subcommands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    trading = subcommands.add_parser("trading", help="inspect Trading facts and record bounded operator intent")
    commands = trading.add_subparsers(dest="trading_command", required=True)
    commands.add_parser("status", help="show Alpha producer and execution readiness")
    diagnose = commands.add_parser("diagnose", help="sample bounded read-only execution evidence")
    diagnose.add_argument("--status-url", help="optional serve /api/trading/status URL")

    cases = commands.add_parser("cases", help="list Trading cases newest first")
    cases.add_argument("--state", choices=("pending", "running", "complete", "failed"), default=None)
    cases.add_argument("--limit", type=_positive_int, default=20)

    signals = commands.add_parser("signals", help="list TradeSignalV4 rows")
    signals.add_argument("--limit", type=_positive_int, default=20)

    fills = commands.add_parser("fills", help="list signed DEMO venue fills")
    fills.add_argument("--limit", type=_positive_int, default=20)

    scoreboard = commands.add_parser("scoreboard", help="compare all Trading policies on LIVE paper legs")
    scoreboard.add_argument("--since", required=True, help="UTC date or ISO timestamp")
    scoreboard.add_argument("--until", required=True, help="exclusive UTC date or ISO timestamp")
    scoreboard.add_argument("--program", help="optional 64-character program sha")

    replay = commands.add_parser("replay", help="assess frozen CaseViews with a candidate DSPy program")
    replay.add_argument("--program", help="candidate DSPy JSON file; required for inference")
    replay.add_argument("--mode", choices=("policies", "inference"), default="policies")
    replay.add_argument("--source-run", help="stored source run; required for policies")
    replay.add_argument("--tag", default="default", help="new tag creates a separate evaluation run")
    replay.add_argument("--since", required=True, help="UTC date or ISO timestamp")
    replay.add_argument("--until", required=True, help="exclusive UTC date or ISO timestamp")

    calibration = commands.add_parser(
        "calibrate", help="fit a local candidate on training labels and compare a future window"
    )
    calibration.add_argument("--source-run", required=True)
    calibration.add_argument("--train-since", required=True, help="UTC training start")
    calibration.add_argument("--train-until", required=True, help="exclusive training/label availability boundary")
    calibration.add_argument("--validate-until", required=True, help="exclusive future validation boundary")
    calibration.add_argument("--output", required=True, help="local calibration JSON; does not activate it")

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


__all__ = ["add_trading_commands"]
