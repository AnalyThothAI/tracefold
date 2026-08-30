from __future__ import annotations

import argparse

from tracefold.app.cli.parsers.common import _nonnegative_int


def add_ops_commands(
    subcommands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    ops = subcommands.add_parser("ops", help="maintenance commands")
    ops_subcommands = ops.add_subparsers(dest="ops_command", required=True)
    validate_projections = ops_subcommands.add_parser(
        "validate-projections",
        help="validate projection read models against PostgreSQL facts",
    )
    validate_projections.add_argument("--sample", type=_nonnegative_int, default=100)
