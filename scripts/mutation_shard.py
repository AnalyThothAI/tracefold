"""Reserve one slice of a mutation session for this worker, by skipping every job that is not its own.

Cosmic Ray's `local` distributor is strictly sequential, and its `http` distributor cannot share a
checkout because mutation happens **in place** — two workers in one tree would read each other's
mutants and report kills that never happened. One checkout per worker is therefore not a tuning
knob, it is the correctness requirement, and a CI job matrix is precisely that: N runners, N
checkouts, no shared state.

So each shard initialises the whole session, marks the jobs belonging to other shards as SKIPPED —
the same mechanism Cosmic Ray's own operator filters use — and executes what is left.
`scripts/mutation_survivors.py` sums the shard databases back into one score.

The slices are assigned by position in a `(module_path, operator_name, occurrence)` ordering, and
that key is the whole correctness argument. Each matrix leg runs its own `cosmic-ray init`, which
mints a fresh `uuid4().hex` for every `job_id`, so ordering by `job_id` — which this did at first —
gives each shard an independent random subset rather than a partition: at six shards roughly a third
of the population would be executed by nobody and another quarter twice over. The identity triple is
derived from the source instead, and is verified unique over the population and byte-identical
across two independent inits.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from cosmic_ray.work_db import WorkDB, use_db
from cosmic_ray.work_item import WorkerOutcome, WorkItem, WorkResult


def _slice_key(item: WorkItem) -> tuple[str, str, int]:
    """A mutation site's identity, derived from the source rather than minted by `init`."""

    mutation = item.mutations[0]
    return (str(mutation.module_path), str(mutation.operator_name), int(mutation.occurrence))


def _reserve(work_db: WorkDB, *, shard: int, of: int) -> tuple[int, int]:
    """Skip every pending job outside this shard. Returns `(kept, skipped)`."""

    pending = sorted(work_db.pending_work_items, key=_slice_key)
    kept = 0
    skipped = 0
    for index, item in enumerate(pending):
        if index % of == shard:
            kept += 1
            continue
        work_db.set_result(
            item.job_id,
            WorkResult(output=f"reserved for shard {index % of}", worker_outcome=WorkerOutcome.SKIPPED),
        )
        skipped += 1
    return kept, skipped


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session", type=Path, help="cosmic-ray session database to narrow in place")
    parser.add_argument("--shard", type=int, required=True, help="zero-based index of this shard")
    parser.add_argument("--of", type=int, required=True, help="total number of shards")
    args = parser.parse_args(argv)

    if not 0 <= args.shard < args.of:
        sys.stderr.write(f"shard {args.shard} is not in range for {args.of} shards\n")
        return 1
    if not args.session.is_file():
        sys.stderr.write(f"no mutation session at {args.session}\n")
        return 1

    with use_db(str(args.session)) as work_db:
        kept, skipped = _reserve(work_db, shard=args.shard, of=args.of)

    sys.stdout.write(f"shard {args.shard}/{args.of}: {kept} mutants to run, {skipped} left to other shards\n")
    return 0 if kept else 1


if __name__ == "__main__":
    sys.exit(main())
