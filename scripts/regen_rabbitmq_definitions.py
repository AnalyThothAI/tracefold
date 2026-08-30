# scripts/regen_rabbitmq_definitions.py
"""Regenerate docker/rabbitmq/definitions.json from `tracefold.news.broker_policy`.

The News retry contract is written once, in code, and imported into RabbitMQ from this document. Keeping
the file generated means the deployed policy and the constant the tests assert can never drift apart.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from tracefold.news.broker_policy import definitions_json

OUTPUT = Path(__file__).resolve().parent.parent / "docker" / "rabbitmq" / "definitions.json"


def main() -> int:
    parser = argparse.ArgumentParser(description="Regenerate docker/rabbitmq/definitions.json")
    parser.add_argument("--check", action="store_true", help="exit non-zero if the file is stale")
    args = parser.parse_args()

    rendered = definitions_json()
    if args.check:
        existing = OUTPUT.read_text(encoding="utf-8") if OUTPUT.exists() else ""
        if existing != rendered:
            print(
                "docker/rabbitmq/definitions.json is stale; run `uv run python scripts/regen_rabbitmq_definitions.py`.",
                file=sys.stderr,
            )
            return 1
        return 0

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(rendered, encoding="utf-8")
    print(f"wrote {OUTPUT.relative_to(OUTPUT.parents[1]).as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
