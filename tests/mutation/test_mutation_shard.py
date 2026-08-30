"""The shard split must partition the population when each shard initialises its own session.

This is the shape the CI matrix actually runs, and it is the shape that caught a real defect: the
first version of `scripts/mutation_shard.py` ordered by `job_id`, which `cosmic-ray init` mints as a
fresh `uuid4().hex` every time. Six shards each running their own `init` therefore took six
independent random sixths — roughly a third of the mutants executed by nobody, a quarter executed
twice — and the earlier check missed it entirely because it sharded six *copies of one database*,
where the job_ids happen to agree.

So the fixture here is deliberately six separate `cosmic-ray init` runs. A test that copies one
session cannot observe the bug it is meant to prevent.

Requires the `mutation` dependency group, which the hermetic lane does not install; this runs in the
mutation workflow's baseline step, where the group is present.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("cosmic_ray", reason="needs the `mutation` dependency group")

from scripts.mutation_shard import _reserve

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SHARDS = 3


def _init(session: Path) -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "cosmic_ray.cli", "init", "mutation.toml", str(session)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:  # pragma: no cover - surfaces a broken config, not a broken shard
        pytest.fail(f"cosmic-ray init failed:\n{completed.stderr}")


def _reserved_keys(session: Path, *, shard: int, of: int) -> set[tuple[str, str, int]]:
    from cosmic_ray.work_db import use_db

    with use_db(str(session)) as work_db:
        _reserve(work_db, shard=shard, of=of)
        return {
            (str(m.module_path), str(m.operator_name), int(m.occurrence))
            for item in work_db.pending_work_items
            for m in item.mutations
        }


@pytest.mark.slow
def test_independently_initialised_shards_partition_the_population(tmp_path: Path) -> None:
    """Disjoint, complete, and balanced to within one — across separate `init` runs."""

    slices = []
    population: set[tuple[str, str, int]] = set()
    for shard in range(SHARDS):
        session = tmp_path / f"shard-{shard}.sqlite"
        _init(session)
        slices.append(_reserved_keys(session, shard=shard, of=SHARDS))
        population |= _all_keys(session)

    union: set[tuple[str, str, int]] = set()
    for reserved in slices:
        assert not (union & reserved), "two shards reserved the same mutation site"
        union |= reserved

    assert union == population, "every mutant must be executed by exactly one shard"
    assert max(len(s) for s in slices) - min(len(s) for s in slices) <= 1


def _all_keys(session: Path) -> set[tuple[str, str, int]]:
    import sqlite3

    with sqlite3.connect(f"file:{session}?mode=ro", uri=True) as connection:
        rows = connection.execute("SELECT module_path, operator_name, occurrence FROM mutation_specs").fetchall()
    return {(str(module), str(operator), int(occurrence)) for module, operator, occurrence in rows}
