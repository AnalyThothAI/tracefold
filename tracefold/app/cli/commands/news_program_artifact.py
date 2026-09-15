"""CLI adapter for regenerating the code-owned News Program artifact."""

from __future__ import annotations

import argparse

from tracefold.news.program.artifact_tool import regenerate_stable_program_state


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.parse_args(argv)
    print(regenerate_stable_program_state())


if __name__ == "__main__":
    main()


__all__ = ["main"]
