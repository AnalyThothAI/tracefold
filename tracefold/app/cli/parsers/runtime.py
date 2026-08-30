from __future__ import annotations

import argparse


def add_runtime_commands(
    subcommands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
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
