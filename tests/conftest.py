"""Repository-wide deterministic Hypothesis profiles."""

from __future__ import annotations

import os

import pytest
from hypothesis import settings

settings.register_profile("fast", max_examples=40, database=None, print_blob=True)
settings.register_profile("ci", max_examples=150, database=None, derandomize=True, print_blob=True)
settings.register_profile("nightly", max_examples=500, database=None, derandomize=False, print_blob=True)
settings.load_profile(os.environ.get("TRACEFOLD_HYPOTHESIS_PROFILE") or "fast")


@pytest.fixture
def synthetic_reader_calibration(monkeypatch: pytest.MonkeyPatch) -> None:
    """Opt-in seam calibration; synthetic evidence is never a release certificate."""

    from tracefold.news.notifications.policy import READER_CALIBRATIONS, ReaderCalibration

    calibration = ReaderCalibration(
        materiality_floor=2,
        push_coefficients=(0.0, 0.0, 1.0, -1.0),
        key_coefficients=(0.0, 1.0, 0.0),
        push_cut=0.65,
        key_cut=0.75,
        certification_status="certified",
    )
    for backend in ("native", "generated"):
        monkeypatch.setitem(READER_CALIBRATIONS, backend, calibration)
