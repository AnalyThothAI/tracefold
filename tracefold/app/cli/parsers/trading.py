from __future__ import annotations

import argparse

from tracefold.app.cli.parsers.common import _positive_int


def add_trading_commands(
    subcommands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    trading = subcommands.add_parser("trading", help="read Alpha Cases, Signals, and execution observations")
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


__all__ = ["add_trading_commands"]
