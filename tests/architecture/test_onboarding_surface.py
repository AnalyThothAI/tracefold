from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MAKE_TARGET_RE = re.compile(r"^(?P<target>[a-zA-Z0-9_-]+):", re.MULTILINE)


def test_one_command_onboarding_has_one_public_lifecycle() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    targets = {match.group("target") for match in MAKE_TARGET_RE.finditer(makefile)}

    assert {"up", "status", "logs", "down"} <= targets
    assert {
        "docker-up",
        "docker-status",
        "docker-logs",
        "docker-down",
    }.isdisjoint(targets)


def test_readme_routes_fresh_clone_to_the_complete_stack() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "make up" in readme
    assert "make docker-up" not in readme
    assert "make sync\nmake init\nmake db-migrate\nmake serve" not in readme
    assert "rsshub.env" not in readme


def test_generated_operator_config_has_no_static_duplicate() -> None:
    assert not (ROOT / "config.example.yaml").exists()
