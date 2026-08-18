from __future__ import annotations

import json
import sys
from decimal import Decimal
from typing import TextIO

from . import parser as cli_parser
from .commands import CommandResult


def main(argv: list[str] | None = None, *, stdout: TextIO = sys.stdout) -> int:
    parser = cli_parser.build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)

    command = args.command or "serve"
    if command == "init":
        from .commands import config

        return _finish(config.handle_init(args), stdout)
    if command == "serve":
        from .commands import serve

        return _finish(serve.handle_serve(args), stdout)
    if command == "workers":
        from .commands import workers

        return _finish(workers.handle_workers(args), stdout)
    if command == "config":
        from .commands import config

        return _finish(config.handle_config(args), stdout)
    if command == "db":
        from .commands import db

        return _finish(db.handle_db(args), stdout)
    if command == "macro":
        from .commands import macro

        return _finish(macro.handle_macro(args), stdout)
    if command == "news":
        from .commands import news

        return _finish(news.handle_news(args), stdout)
    if command == "ops":
        from .commands import ops

        return _finish(ops.handle_ops(args, parser), stdout)

    parser.error(f"unknown command: {command}")
    return 2


def _finish(result: CommandResult, stdout: TextIO) -> int:
    if isinstance(result, int):
        return result
    exit_code, payload = result
    _emit(payload, stdout)
    return exit_code


def _emit(payload: dict, stdout: TextIO) -> None:
    stdout.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=_json_default) + "\n")


def _json_default(value: object) -> object:
    if isinstance(value, Decimal):
        return float(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")
