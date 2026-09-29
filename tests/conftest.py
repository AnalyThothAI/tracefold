"""Repository-wide deterministic Hypothesis profiles."""

from __future__ import annotations

import os

from hypothesis import settings

settings.register_profile("fast", max_examples=40, database=None, print_blob=True)
settings.register_profile("ci", max_examples=150, database=None, derandomize=True, print_blob=True)
settings.register_profile("nightly", max_examples=500, database=None, derandomize=False, print_blob=True)
settings.load_profile(os.environ.get("TRACEFOLD_HYPOTHESIS_PROFILE") or "fast")
