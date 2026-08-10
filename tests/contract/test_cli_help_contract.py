from __future__ import annotations

import argparse
import re
from pathlib import Path

import pytest

from tracefold.app.cli.parser import build_parser

ROOT = Path(__file__).resolve().parents[2]
CLI_HELP_PATH = ROOT / "docs" / "generated" / "cli-help.md"
SECTION_RE = re.compile(
    r"^## (?P<title>Top level|`[^`]+`)\n\n```\n(?P<body>.*?)^```$",
    re.MULTILINE | re.DOTALL,
)


def _command_parsers(
    parser: argparse.ArgumentParser,
    prefix: tuple[str, ...] = (),
) -> list[tuple[str, argparse.ArgumentParser]]:
    commands: list[tuple[str, argparse.ArgumentParser]] = []
    for action in parser._actions:
        if not isinstance(action, argparse._SubParsersAction):
            continue
        for name, child in action.choices.items():
            path = (*prefix, name)
            commands.append((" ".join(path), child))
            commands.extend(_command_parsers(child, path))
    return commands


@pytest.mark.contract
def test_generated_cli_help_covers_every_command_and_leaf_option() -> None:
    parser = build_parser()
    expected = {"Top level": parser.format_help().rstrip()}
    expected.update({path: child.format_help().rstrip() for path, child in _command_parsers(parser)})

    generated = {
        match.group("title").strip("`"): match.group("body").rstrip()
        for match in SECTION_RE.finditer(CLI_HELP_PATH.read_text(encoding="utf-8"))
    }

    assert generated == expected
