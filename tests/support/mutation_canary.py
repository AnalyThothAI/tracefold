"""A deliberately trivial function whose every mutation is observable.

`scripts/mutation_sentinel.py` mutates this module and requires that *nothing* survives. That is the
only check that distinguishes a working mutation lane from one that reports a perfect score because
its mutants never reached the interpreter — the failure mode that ruled mutmut out for this repo,
where the shadow `mutants/` tree was importable as a namespace package and the tests kept importing
the original source.

Keep this pure, dependency-free and fully pinned by `tests/mutation/test_mutation_canary.py`. If a
mutation of this file can survive, the survivor is a bug in the harness, not in the canary.
"""

from __future__ import annotations

CANARY_SCALE = 137


def canary_bps(value: int) -> int:
    """`value * CANARY_SCALE + 1`, chosen so every arithmetic and constant mutation changes the result."""

    return value * CANARY_SCALE + 1
