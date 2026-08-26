"""Reader-history projections shared by Triage load and settle phases."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from ..program.contracts import TriageContext
from ..reader_history import ReaderHistorySnapshot


def _read_history(news: Any, *, event_id: str, card: Mapping[str, Any], now_ms: int) -> ReaderHistorySnapshot:
    return cast(
        ReaderHistorySnapshot,
        news.reader_history(
            event_id=event_id,
            now_ms=now_ms,
            include_targeted=str(card.get("admission") or "")
            not in {"telemetry_deterministic", "liquidation_deterministic"},
        ),
    )


def _recent_seen(history: ReaderHistorySnapshot) -> list[dict[str, Any]]:
    return [row.as_told_row() for row in history.recent_seen_rows]


def _novelty_context_sha(card: Mapping[str, Any], history: ReaderHistorySnapshot, *, now_ms: int) -> str:
    context = TriageContext.from_card(
        card,
        watchlist=(),
        told_rows=[row.as_told_row() for row in history.told_source_rows],
        now_ms=now_ms,
        queue_lag_ms=0,
    )
    return context.novelty_context_sha256()


__all__ = ["_novelty_context_sha", "_read_history", "_recent_seen"]
