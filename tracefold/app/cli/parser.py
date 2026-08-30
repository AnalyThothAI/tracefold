from __future__ import annotations

import argparse

from tracefold.app.cli.parsers.database import add_database_commands
from tracefold.app.cli.parsers.news import add_news_commands
from tracefold.app.cli.parsers.ops import add_ops_commands
from tracefold.app.cli.parsers.runtime import add_runtime_commands
from tracefold.app.cli.parsers.trading import add_trading_commands


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tracefold")
    subcommands = parser.add_subparsers(dest="command")
    add_runtime_commands(subcommands)
    add_database_commands(subcommands)
    add_news_commands(subcommands)
    add_trading_commands(subcommands)
    add_ops_commands(subcommands)
    return parser
