"""Pins `canary_bps` exactly, so any mutation of it fails this test.

This runs in the ordinary suite as well as under the sentinel: if the canary's own contract ever
drifts from the function, the sentinel's "zero survivors" requirement would start failing for a
reason that has nothing to do with the mutation harness.
"""

from __future__ import annotations

import pytest

from tests.support.mutation_canary import CANARY_SCALE, canary_bps


def test_the_canary_scale_is_the_pinned_constant() -> None:
    assert CANARY_SCALE == 137


@pytest.mark.parametrize(("value", "expected"), [(0, 1), (1, 138), (2, 275), (-1, -136)])
def test_canary_bps_is_value_times_scale_plus_one(value: int, expected: int) -> None:
    assert canary_bps(value) == expected
