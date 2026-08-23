"""Concrete Market Review repositories assembled from lifecycle-owned storage modules."""

from __future__ import annotations

from typing import Any

from .instrument_storage import InstrumentsRepository, SnapshotResult
from .quote_storage import QuoteStorage
from .review_storage import MarketReviewCohort, ReviewStorage


class PriceRepository(QuoteStorage, ReviewStorage):
    def __init__(self, conn: Any) -> None:
        self.conn = conn


__all__ = ["InstrumentsRepository", "MarketReviewCohort", "PriceRepository", "SnapshotResult"]
