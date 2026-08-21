from __future__ import annotations

import fcntl
import os
import subprocess
import sys
from pathlib import Path


def _git_common_dir() -> Path:
    result = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        check=True,
        capture_output=True,
        text=True,
    )
    return Path(result.stdout.strip())


def main(arguments: list[str]) -> int:
    if not arguments:
        print("usage: with_deployment_lock.py COMMAND [ARG ...]", file=sys.stderr)
        return 2
    lock_path = _git_common_dir() / "tracefold-deployment.lock"
    with lock_path.open("a+b") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Tracefold deployment is already in progress.", file=sys.stderr)
            return 2
        environment = {**os.environ, "TRACEFOLD_DEPLOY_LOCK_HELD": "1"}
        os.set_inheritable(lock_file.fileno(), True)
        os.execvpe(arguments[0], arguments, environment)  # noqa: S606 -- exact argv; no shell expansion


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
