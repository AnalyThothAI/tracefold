"""Run a staged frontend hook from the web project with repo-relative paths normalized."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB_ROOT = ROOT / "web"


def main(arguments: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if arguments is None else arguments)
    if not args or args[0] not in {"eslint", "prettier"}:
        print("usage: run_web_hook.py {eslint|prettier} FILE...", file=sys.stderr)
        return 2
    tool, *raw_paths = args
    paths: list[str] = []
    for raw_path in raw_paths:
        path = (ROOT / raw_path).resolve()
        if not path.is_relative_to(WEB_ROOT):
            print(f"frontend hook refused path outside web/: {raw_path}", file=sys.stderr)
            return 2
        paths.append(path.relative_to(WEB_ROOT).as_posix())
    if not paths:
        return 0

    executable = WEB_ROOT / "node_modules" / ".bin" / tool
    if not executable.is_file():
        print("frontend hook dependencies are missing; run `npm ci --prefix web`", file=sys.stderr)
        return 2
    options = ["--max-warnings=0", "--no-warn-ignored"] if tool == "eslint" else ["--check"]
    return subprocess.run([str(executable), *options, *paths], cwd=WEB_ROOT, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
