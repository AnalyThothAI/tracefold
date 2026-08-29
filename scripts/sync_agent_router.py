#!/usr/bin/env python3
"""Generate both root routers from the canonical shared routing block.

`--write` regenerates the block from `docs/agents/shared-router.md`; `--check`
reports drift and is owned by `make check`.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CANONICAL = ROOT / "docs" / "agents" / "shared-router.md"
ROUTERS = (ROOT / "AGENTS.md", ROOT / "CLAUDE.md")
BEGIN = "<!-- BEGIN SHARED AGENT ROUTER -->"
END = "<!-- END SHARED AGENT ROUTER -->"


def shared_block(text: str, *, source: Path) -> str:
    """The bytes between the two markers, markers excluded. Exactly one pair, or it is an error."""

    if text.count(BEGIN) != 1 or text.count(END) != 1:
        raise ValueError(f"{source.relative_to(ROOT)}: expected exactly one shared-router marker pair")
    head, rest = text.split(BEGIN, 1)
    body, tail = rest.split(END, 1)
    del head, tail
    return body


def rendered(router_text: str, canonical_body: str, *, source: Path) -> str:
    head, rest = router_text.split(BEGIN, 1)
    _, tail = rest.split(END, 1)
    del source
    return f"{head}{BEGIN}{canonical_body}{END}{tail}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--write", action="store_true", help="regenerate the shared block in both routers")
    group.add_argument("--check", action="store_true", help="fail if either router drifted from the source")
    args = parser.parse_args()

    canonical_body = shared_block(CANONICAL.read_text(encoding="utf-8"), source=CANONICAL)
    drifted: list[str] = []
    for router in ROUTERS:
        current = router.read_text(encoding="utf-8")
        shared_block(current, source=router)  # reject a router with a broken marker pair
        expected = rendered(current, canonical_body, source=router)
        if current == expected:
            continue
        if args.check:
            drifted.append(router.relative_to(ROOT).as_posix())
            continue
        router.write_text(expected, encoding="utf-8")
        print(f"updated {router.relative_to(ROOT).as_posix()}")

    if drifted:
        print(
            "shared agent router drifted in: "
            + ", ".join(drifted)
            + f". Edit {CANONICAL.relative_to(ROOT).as_posix()} and run "
            "`uv run python scripts/sync_agent_router.py --write`.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
