"""Deterministic reader judges for notification seam tests: one fixed incremental importance for every claim."""

from __future__ import annotations

from tracefold.news.updates.judgment import Budget
from tracefold.news.updates.reader_judgments import (
    AnchorEvidence,
    ImportanceEvidence,
    ReaderInput,
    ReaderJudgment,
    anchor_options,
)


class FixedReader:
    """Answers every claim with one importance value and no anchor; counts what it was asked."""

    identity = "fixture_reader_fixed"

    def __init__(self, value: float = 2.6) -> None:
        self.value = value
        self.asked: list[ReaderInput] = []

    async def judge(self, reader: ReaderInput, budget: Budget) -> ReaderJudgment:
        self.asked.append(reader)
        options = anchor_options(len(reader.messages)) if reader.messages else ()
        return ReaderJudgment(
            status="available",
            backend="native",
            identity=self.identity,
            importance=ImportanceEvidence(value=self.value, probabilities=_distribution(self.value), confidence=0.8),
            anchor=None
            if not options
            else AnchorEvidence(
                probabilities={value: 1.0 if value == "none" else 0.0 for value, _ in options}, confidence=0.9
            ),
        )


class PushAll(FixedReader):
    identity = "fixture_reader_push_all"

    def __init__(self) -> None:
        super().__init__(2.6)


class FeedOnly(FixedReader):
    identity = "fixture_reader_feed_only"

    def __init__(self) -> None:
        super().__init__(1.0)


class Unavailable:
    identity = "fixture_reader_unavailable"

    def __init__(self) -> None:
        self.calls = 0

    async def judge(self, reader: ReaderInput, budget: Budget) -> ReaderJudgment:
        self.calls += 1
        return ReaderJudgment(status="unavailable", error_code="fixture_reader_down")


def _distribution(value: float) -> tuple[float, ...]:
    low = int(value)
    high = min(4, low + 1)
    weight = value - low
    levels = [0.0] * 5
    levels[low] += 1 - weight
    levels[high] += weight
    return tuple(levels)
