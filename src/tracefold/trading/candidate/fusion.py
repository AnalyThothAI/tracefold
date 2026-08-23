"""Point-in-time fusion of eligible News and OI candidates."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from ..contracts import CaseKind, NewsTradeCandidate, OiTradeCandidate, underlying_key
from .eligibility import DEFAULT_ELIGIBILITY, EligibilityPolicy


def attach_news(
    candidate: OiTradeCandidate,
    news: Sequence[NewsTradeCandidate],
    *,
    policy: EligibilityPolicy = DEFAULT_ELIGIBILITY,
) -> NewsTradeCandidate | None:
    """The newest eligible News for the same underlying, strictly at or before the OI trigger.

    Point-in-time only: a News verdict written after the frame is the future, and attaching it would
    make every replay of that case unreproducible.
    """

    key = underlying_key(candidate.base_symbol)
    window_start = candidate.observed_at_ms - policy.news_lookback_ms
    matches = [
        item
        for item in news
        if underlying_key(item.base_symbol) == key
        and window_start <= item.verdict_created_at_ms <= candidate.observed_at_ms
    ]
    return max(matches, key=lambda item: item.verdict_created_at_ms) if matches else None


def attach_oi(
    candidate: NewsTradeCandidate,
    signals: Sequence[OiTradeCandidate],
    *,
    policy: EligibilityPolicy = DEFAULT_ELIGIBILITY,
) -> OiTradeCandidate | None:
    """The newest qualifying OI frame for the same underlying, strictly at or before the verdict."""

    key = underlying_key(candidate.base_symbol)
    window_start = candidate.verdict_created_at_ms - policy.oi_lookback_ms
    matches = [
        item
        for item in signals
        if underlying_key(item.base_symbol) == key
        and window_start <= item.observed_at_ms <= candidate.verdict_created_at_ms
    ]
    return max(matches, key=lambda item: item.observed_at_ms) if matches else None


@dataclass(frozen=True, slots=True)
class _Plan:
    """One underlying's worth of work, already reduced to at most one candidate."""

    kind: CaseKind
    base_symbol: str
    observed_at_ms: int
    oi: OiTradeCandidate | None
    news: NewsTradeCandidate | None
    source_key: str
    supplemental: tuple[str, ...]


def _fuse(
    oi: OiTradeCandidate | None,
    news: NewsTradeCandidate | None,
    *,
    policy: EligibilityPolicy,
) -> _Plan | None:
    """Reduce one underlying to one plan; the counterpart must be inside lookback and before the cutoff."""

    if news is None:
        if oi is None:  # pragma: no cover - the caller only passes keys that have at least one side
            return None
        return _Plan(
            kind="oi_only",
            base_symbol=oi.base_symbol,
            observed_at_ms=oi.observed_at_ms,
            oi=oi,
            news=None,
            source_key=oi.source_key,
            supplemental=(),
        )
    if oi is None:
        return _Plan(
            kind="news_only",
            base_symbol=news.base_symbol,
            observed_at_ms=news.verdict_created_at_ms,
            oi=None,
            news=news,
            source_key=news.source_key,
            supplemental=(),
        )

    if news.verdict_created_at_ms <= oi.observed_at_ms:
        attached = attach_news(oi, [news], policy=policy)
        return _Plan(
            kind="news_oi" if attached is not None else "oi_only",
            base_symbol=oi.base_symbol,
            observed_at_ms=oi.observed_at_ms,
            oi=oi,
            news=attached,
            source_key=oi.source_key,
            supplemental=(attached.source_key,) if attached is not None else (),
        )
    attached_oi = attach_oi(news, [oi], policy=policy)
    return _Plan(
        kind="news_oi" if attached_oi is not None else "news_only",
        base_symbol=news.base_symbol,
        observed_at_ms=news.verdict_created_at_ms,
        oi=attached_oi,
        news=news,
        source_key=news.source_key,
        supplemental=(attached_oi.source_key,) if attached_oi is not None else (),
    )


__all__ = ["attach_news", "attach_oi"]
