from __future__ import annotations

import sys
from typing import TextIO


def main(argv: list[str] | None = None, *, stdout: TextIO = sys.stdout) -> int:
    from .app.cli.main import main as run_cli

    return run_cli(argv, stdout=stdout)


__all__ = ["main"]
