"""The one concrete Market Review repository that is composed rather than owned by a storage module."""

from __future__ import annotations

from typing import Any

from .quote_storage import QuoteStorage
from .review_storage import MarketReviewCohort, ReviewStorage


class PriceRepository(QuoteStorage, ReviewStorage):
    def __init__(self, conn: Any) -> None:
        self.conn = conn


__all__ = ["MarketReviewCohort", "PriceRepository"]
