"""Repository-wide deterministic Hypothesis profiles, and one import-order guard.

The guard: `dspy` installs a lazy proxy over `numpy` when it is imported first, and `pyarrow` — which
`nautilus_trader` loads — initialises its Cython extension against `numpy._core.multiarray` and fails
against that proxy. Whether a session survives therefore depended on which test module happened to be
collected first, which is a coin flip the collection order decides. Importing the real `numpy` here,
before any test module, makes the whole lane order-independent.
"""

from __future__ import annotations

import os

import numpy  # noqa: F401  # see the module docstring: must precede any dspy import
from hypothesis import settings

settings.register_profile("fast", max_examples=40, stateful_step_count=20, database=None, print_blob=True)
settings.register_profile(
    "ci", max_examples=150, stateful_step_count=50, database=None, derandomize=True, print_blob=True
)
settings.register_profile(
    "nightly", max_examples=500, stateful_step_count=100, database=None, derandomize=False, print_blob=True
)
settings.load_profile(os.environ.get("TRACEFOLD_HYPOTHESIS_PROFILE") or "fast")
