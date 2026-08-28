"""News-owned frozen projection for one delivered Telegram trade entry point."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TelegramManualTradeProjectionV1:
    """The exact News facts App may map into Trading after a sent Telegram receipt."""

    projection_version: str
    event_id: str
    opened_at_ms: int
    final_decision: str
    degraded: bool
    direction: str
    title_zh: str
    primary_assets: tuple[str, ...]
    grounded_assets: tuple[str, ...]


__all__ = ["TelegramManualTradeProjectionV1"]
