"""The shard split must partition the population when each shard initialises its own session.

This is the shape the CI matrix actually runs, and it is the shape that caught a real defect: the
first version of `scripts/mutation_shard.py` ordered by `job_id`, which `cosmic-ray init` mints as a
fresh `uuid4().hex` every time. Six shards each running their own `init` therefore took six
independent random sixths — roughly a third of the mutants executed by nobody, a quarter executed
twice — and the earlier check missed it entirely because it sharded six *copies of one database*,
where the job_ids happen to agree. So the fixture here is one `cosmic-ray init` per shard; a test
that copies one session cannot observe the bug it is meant to prevent.

Two things about how this is wired, both of which were wrong first. It is marked `scheduled` so the
required selections exclude it outright: `require_test_reports.py` rejects a *skipped* test inside a
required report, which is the right rule — a skip is how a suite quietly stops covering something.
And the `cosmic_ray` import lives inside the test rather than at module scope, because
`pytest.importorskip` at import time records that skip during collection, before any marker
filtering can deselect the module — so the marker alone did not keep it out of the required lanes.

The mutation workflow runs `tests/mutation` with the `mutation` group installed, which is where this
actually executes.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.scheduled

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

    from scripts.mutation_shard import _reserve

    with use_db(str(session)) as work_db:
        _reserve(work_db, shard=shard, of=of)
        return {
            (str(m.module_path), str(m.operator_name), int(m.occurrence))
            for item in work_db.pending_work_items
            for m in item.mutations
        }


def _all_keys(session: Path) -> set[tuple[str, str, int]]:
    with sqlite3.connect(f"file:{session}?mode=ro", uri=True) as connection:
        rows = connection.execute("SELECT module_path, operator_name, occurrence FROM mutation_specs").fetchall()
    return {(str(module), str(operator), int(occurrence)) for module, operator, occurrence in rows}


def test_independently_initialised_shards_partition_the_population(tmp_path: Path) -> None:
    """Disjoint, complete, and balanced to within one — across separate `init` runs."""

    pytest.importorskip("cosmic_ray", reason="needs the `mutation` dependency group")

    slices = []
    populations = []
    for shard in range(SHARDS):
        session = tmp_path / f"shard-{shard}.sqlite"
        _init(session)
        slices.append(_reserved_keys(session, shard=shard, of=SHARDS))
        populations.append(_all_keys(session))

    # Checked before the partition, because otherwise its failure is a set difference nobody can
    # read. The shards can only partition a population they agree on, and the one way they disagree
    # is a batch mutating the same modules while this runs — Cosmic Ray rewrites the source in
    # place, so `init` at one moment and `init` a second later can genuinely see different code.
    assert all(p == populations[0] for p in populations), (
        "the inits disagreed about the mutant population, so there is nothing to partition. "
        "A mutation batch rewriting these modules concurrently is the usual cause."
    )

    population = populations[0]
    union: set[tuple[str, str, int]] = set()
    for reserved in slices:
        assert not (union & reserved), "two shards reserved the same mutation site"
        union |= reserved

    assert union == population, "every mutant must be executed by exactly one shard"
    assert max(len(s) for s in slices) - min(len(s) for s in slices) <= 1
