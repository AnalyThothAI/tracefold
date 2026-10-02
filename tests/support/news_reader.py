"""Synthetic independent reader evidence for notification seam tests."""

from __future__ import annotations

from tracefold.news.notifications.reader import (
    REPORT_KIND_OPTIONS,
    AnchorEvidence,
    InterruptEvidence,
    MaterialityEvidence,
    ReaderInput,
    ReaderJudgment,
    ReportKind,
    ReportKindEvidence,
    anchor_options,
)
from tracefold.news.updates.judgment import Budget


class FixedReader:
    """Returns a fixed materiality, report kind and interrupt probability; records exact inputs."""

    identity = "fixture_reader_fixed"

    def __init__(
        self, value: float = 2.6, anchor: str = "none", *, kind: ReportKind = "new_action", interrupt: float = 0.05
    ) -> None:
        self.value = value
        self.anchor = anchor
        self.kind = kind
        self.interrupt = interrupt
        self.asked: list[ReaderInput] = []

    async def judge(self, reader: ReaderInput, budget: Budget) -> ReaderJudgment:
        self.asked.append(reader)
        options = anchor_options(len(reader.messages)) if reader.messages else ()
        return ReaderJudgment(
            status="available",
            backend="native",
            identity=self.identity,
            report_kind=ReportKindEvidence(
                value=self.kind,
                probabilities={value: 1.0 if value == self.kind else 0.0 for value, _ in REPORT_KIND_OPTIONS},
                confidence=0.8,
            ),
            materiality=MaterialityEvidence(value=self.value, probabilities=_distribution(self.value), confidence=0.8),
            interrupt=InterruptEvidence(probabilities=(1 - self.interrupt, self.interrupt), confidence=0.8),
            anchor=None
            if not options
            else AnchorEvidence(
                probabilities={value: 1.0 if value == self.anchor else 0.0 for value, _ in options}, confidence=0.9
            ),
        )


class PushAll(FixedReader):
    identity = "fixture_reader_push_all"


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
    high = min(3, low + 1)
    weight = value - low
    levels = [0.0] * 4
    levels[low] += 1 - weight
    levels[high] += weight
    return tuple(levels)
