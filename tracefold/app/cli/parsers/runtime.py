from __future__ import annotations

import argparse


def add_runtime_commands(
    subcommands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    subcommands.add_parser("serve", help="run the read-only HTTP and frontend runtime")
    subcommands.add_parser("workers", help="run the News ingestion, triage, and delivery runtime")
    nautilus = subcommands.add_parser("nautilus", help="run the single OI Nautilus Runtime")
    nautilus_subcommands = nautilus.add_subparsers(dest="nautilus_command", required=True)
    nautilus_subcommands.add_parser("run", help="run the configured disabled, Binance Demo, or Binance Live Runtime")

    init = subcommands.add_parser("init", help="create ~/.tracefold/config.yaml")
    init.add_argument("--force", action="store_true", help="overwrite existing config.yaml")

    subcommands.add_parser("config", help="print effective runtime configuration")
