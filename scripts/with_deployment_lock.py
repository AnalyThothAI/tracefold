from __future__ import annotations

import fcntl
import os
import subprocess
import sys
from pathlib import Path

_LOCK_FD_ENV = "TRACEFOLD_DEPLOY_LOCK_FD"


def _git_common_dir() -> Path:
    result = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        check=True,
        capture_output=True,
        text=True,
    )
    return Path(result.stdout.strip())


def _lock_path() -> Path:
    return _git_common_dir() / "tracefold-deployment.lock"


def assert_inherited_lock() -> int:
    raw_fd = os.environ.get(_LOCK_FD_ENV, "")
    try:
        lock_fd = int(raw_fd)
    except ValueError:
        print("deployment lock verification refused: inherited lock fd is missing", file=sys.stderr)
        return 2
    if lock_fd < 0:
        print("deployment lock verification refused: inherited lock fd is invalid", file=sys.stderr)
        return 2

    lock_path = _lock_path()
    try:
        inherited = os.fstat(lock_fd)
        expected = lock_path.stat()
    except OSError as exc:
        print(f"deployment lock verification refused: {exc}", file=sys.stderr)
        return 2
    if (inherited.st_dev, inherited.st_ino) != (expected.st_dev, expected.st_ino):
        print("deployment lock verification refused: inherited fd is not this repository's lock", file=sys.stderr)
        return 2

    with lock_path.open("a+b") as probe:
        try:
            fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        fcntl.flock(probe.fileno(), fcntl.LOCK_UN)
    print("deployment lock verification refused: inherited fd does not hold the lock", file=sys.stderr)
    return 2


def main(arguments: list[str]) -> int:
    if arguments == ["--assert-held"]:
        return assert_inherited_lock()
    if not arguments:
        print("usage: with_deployment_lock.py COMMAND [ARG ...]", file=sys.stderr)
        return 2
    lock_path = _lock_path()
    with lock_path.open("a+b") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Tracefold deployment is already in progress.", file=sys.stderr)
            return 2
        os.set_inheritable(lock_file.fileno(), True)
        environment = {**os.environ, _LOCK_FD_ENV: str(lock_file.fileno())}
        os.execvpe(arguments[0], arguments, environment)  # noqa: S606 -- exact argv; no shell expansion


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
