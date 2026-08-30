"""News-owned frozen projection for one delivered Telegram trade entry point."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

_DIRECTION_LABELS = frozenset({"利多", "利空", "中性", "不明确", "方向待定"})
_NOVELTY_LABELS = frozenset({"新事实", "新进展", "复述"})
_MAGNITUDE_LABELS = frozenset({"影响很小", "影响有限", "影响明显", "影响重大"})
_TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_TICKER_RE = re.compile(r"^(?:[A-Z0-9][A-Z0-9.-]{0,19}|0x[0-9A-Fa-f]{40})$")
_REPORTING_ORIGIN_RE = re.compile(r"^(?P<origin>.+)（(?P<count>[1-9][0-9]*) 条报道）$")


@dataclass(frozen=True, slots=True)
class TelegramCardFacts:
    direction: str = ""
    novelty: str = ""
    magnitude: str = ""
    assets: tuple[str, ...] = ()
    origin: str = ""
    report_count: int | None = None


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
    displayed_assets: tuple[str, ...]


def displayed_assets_from_telegram_card(card: Mapping[str, Any]) -> tuple[str, ...]:
    """Read only the assets rendered into Telegram's explicit target section.

    The persisted delivery card is the durable input to the Telegram renderer.  Its final non-market
    markdown line is the structured facts line whose asset segment becomes one or more ``标的`` blocks;
    headlines and explanatory prose are intentionally never scanned for ticker-looking words.
    """

    content_lines: list[str] = []
    elements = card.get("elements")
    if isinstance(elements, Sequence) and not isinstance(elements, str | bytes):
        for element in elements:
            if not isinstance(element, Mapping) or element.get("tag") != "markdown":
                continue
            content = str(element.get("content") or "").strip()
            if content:
                content_lines.extend(line.strip() for line in content.splitlines() if line.strip())
    market_line = next((line for line in reversed(content_lines) if line.startswith("行情 ")), "")
    if market_line:
        content_lines.remove(market_line)
    facts_line = content_lines[-1] if content_lines else ""
    return telegram_card_facts(facts_line).assets


def telegram_card_facts(value: str) -> TelegramCardFacts:
    """Parse the one positional facts line shared by the News card and Telegram renderer."""

    parts = [part.strip() for part in str(value or "").split(" · ") if part.strip()]
    if not parts:
        return TelegramCardFacts()
    if _TIME_RE.fullmatch(parts[-1]):
        parts.pop()
    origin = parts.pop() if parts else ""
    report_count: int | None = None
    match = _REPORTING_ORIGIN_RE.fullmatch(origin)
    if match is not None:
        origin = match.group("origin")
        report_count = int(match.group("count"))
    direction = parts.pop(0) if parts and parts[0] in _DIRECTION_LABELS else ""
    novelty = parts.pop(0) if parts and parts[0] in _NOVELTY_LABELS else ""
    magnitude = parts.pop(0) if parts and parts[0] in _MAGNITUDE_LABELS else ""
    assets = (token for token in " ".join(parts).split() if _TICKER_RE.fullmatch(token) is not None)
    return TelegramCardFacts(
        direction=direction,
        novelty=novelty,
        magnitude=magnitude,
        assets=tuple(dict.fromkeys(assets)),
        origin=origin if origin != "-" else "",
        report_count=report_count,
    )


__all__ = [
    "TelegramCardFacts",
    "TelegramManualTradeProjectionV1",
    "displayed_assets_from_telegram_card",
    "telegram_card_facts",
]
