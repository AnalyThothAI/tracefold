from pathlib import Path

import pytest

from scripts.regen_cli_help import render_cli_help

ROOT = Path(__file__).resolve().parents[2]
CLI_HELP_PATH = ROOT / "docs" / "generated" / "cli-help.md"


@pytest.mark.contract
@pytest.mark.generated
def test_generated_cli_help_matches_canonical_renderer() -> None:
    assert CLI_HELP_PATH.read_text(encoding="utf-8") == render_cli_help()
