"""Prove the mutation harness actually executes mutated code before trusting any score.

A mutation score is only evidence if the mutants reach the interpreter. The obvious failure is
silent and reports *good* news: if the tests import the original source while the harness mutates a
copy, every mutant survives the run untouched and the lane looks like it has nothing to say. This is
not hypothetical — it is why `mutmut` was rejected here (see `docs/DEVELOPMENT.md`): its shadow
`mutants/` tree was importable as a namespace package alongside the real one, so the suite kept
importing unmutated `tracefold`.

So: mutate `tests/support/mutation_canary.py`, whose every mutation is pinned by
`tests/mutation/test_mutation_canary.py`, and require that **nothing survives**. A survivor here
means the harness is not delivering mutated code, and the batch score that follows is meaningless.
"""

from __future__ import annotations

import argparse
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CANARY_MODULE = "tests/support/mutation_canary.py"
CANARY_TEST = "tests/mutation/test_mutation_canary.py"
_SESSION_CONFIG = """\
[cosmic-ray]
module-path = "{module}"
timeout = 30.0
excluded-modules = []
test-command = "env TRACEFOLD_HYPOTHESIS_PROFILE=ci uv run --no-sync python -m pytest -x -q -p no:cacheprovider {test}"

[cosmic-ray.distributor]
name = "local"
"""


def _counts(session: Path) -> tuple[int, int]:
    """`(total, survived)` straight out of the session database."""

    with sqlite3.connect(session) as connection:
        total = connection.execute("SELECT count(*) FROM mutation_specs").fetchone()[0]
        survived = connection.execute("SELECT count(*) FROM work_results WHERE test_outcome = 'SURVIVED'").fetchone()[0]
    return int(total), int(survived)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)

    with tempfile.TemporaryDirectory() as workspace:
        config = Path(workspace) / "sentinel.toml"
        session = Path(workspace) / "sentinel.sqlite"
        config.write_text(_SESSION_CONFIG.format(module=CANARY_MODULE, test=CANARY_TEST), encoding="utf-8")

        for stage in ("init", "exec"):
            completed = subprocess.run(
                ["cosmic-ray", stage, str(config), str(session)],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            if completed.returncode != 0:
                sys.stderr.write(f"sentinel: `cosmic-ray {stage}` failed\n{completed.stderr}")
                return 1

        total, survived = _counts(session)

    if total == 0:
        sys.stderr.write(f"sentinel: no mutants were generated for {CANARY_MODULE}; the harness saw nothing\n")
        return 1
    if survived:
        sys.stderr.write(
            f"sentinel: {survived} of {total} canary mutants SURVIVED.\n"
            f"Every mutation of {CANARY_MODULE} is pinned by {CANARY_TEST}, so a survivor means the\n"
            "tests did not import the mutated module. Any mutation score from this harness is invalid.\n"
        )
        return 1

    sys.stdout.write(f"sentinel: {total}/{total} canary mutants killed; the harness executes mutated code\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
