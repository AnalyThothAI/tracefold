"""Repository-wide deterministic Hypothesis profiles."""

from __future__ import annotations

import os

from hypothesis import settings

settings.register_profile("fast", max_examples=40, stateful_step_count=20, database=None, print_blob=True)
settings.register_profile("ci", max_examples=150, stateful_step_count=50, database=None, print_blob=True)
settings.register_profile("nightly", max_examples=500, stateful_step_count=100, database=None, print_blob=True)
settings.load_profile(
    os.environ.get("TRACEFOLD_HYPOTHESIS_PROFILE")
    or ("ci" if os.environ.get("TRACEFOLD_TEST_EVIDENCE") == "1" else "fast")
)
