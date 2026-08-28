"""Post-delivery verification that a single-name ticker maps to a live tradeable contract."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any, Final, Literal, Protocol

from pydantic import Field, model_validator

from .models import ExactNewsModel

TradabilityVenue = Literal["binance", "hyperliquid", "okx", "lighter", "bitget"]
TradabilityState = Literal["matched", "absent", "incomplete"]

REQUIRED_TRADABILITY_VENUES: Final[tuple[TradabilityVenue, ...]] = (
    "binance",
    "hyperliquid",
    "okx",
    "lighter",
    "bitget",
)
TRADABILITY_REVIEW_TIMEOUT_SECONDS: Final[float] = 25.0

_HK_CODE_RE = re.compile(r"(?<!\d)0*(?P<code>\d{4,5})\.HK\b", re.IGNORECASE)
_ASCII_COMPANY_RE = re.compile(r"(?P<name>[A-Za-z][A-Za-z0-9 .&-]{1,60})\s*[（(]")
_SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9./:_-]{0,31}$")


class TradabilityMatch(ExactNewsModel):
    """One exact live catalogue entry; ``price_symbol`` is the provider's market-data key."""

    requested_symbol: str = Field(min_length=1, max_length=32)
    venue_family: TradabilityVenue
    venue: str = Field(min_length=1, max_length=40)
    venue_symbol: str = Field(min_length=1, max_length=64)
    price_symbol: str = Field(min_length=1, max_length=64)
    base_symbol: str = Field(min_length=1, max_length=32)
    quote_asset: str | None = Field(default=None, max_length=16)
    instrument_class: str = Field(default="unknown", min_length=1, max_length=32)


class TradabilityReview(ExactNewsModel):
    """Authoritative five-venue outcome used to choose edit, keep, or delete."""

    state: TradabilityState
    candidates: tuple[str, ...] = Field(max_length=24)
    checked_venues: tuple[TradabilityVenue, ...] = Field(max_length=5)
    failed_venues: tuple[TradabilityVenue, ...] = Field(max_length=5)
    matches: tuple[TradabilityMatch, ...] = Field(max_length=20)
    reason_zh: str = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def validate_outcome(self) -> TradabilityReview:
        required = set(REQUIRED_TRADABILITY_VENUES)
        checked = set(self.checked_venues)
        failed = set(self.failed_venues)
        if checked & failed:
            raise ValueError("tradability_review_venue_overlap")
        if self.state == "matched" and not self.matches:
            raise ValueError("tradability_review_match_missing")
        if self.state == "absent" and (self.matches or checked != required or failed):
            raise ValueError("tradability_review_absence_not_authoritative")
        if self.state == "incomplete" and not failed and checked == required:
            raise ValueError("tradability_review_incomplete_without_gap")
        return self


class TradabilityVerifier(Protocol):
    async def review(
        self,
        *,
        event: Mapping[str, Any],
        verdict: Mapping[str, Any],
        symbols: Sequence[str],
    ) -> TradabilityReview: ...


def tradability_candidates(
    *,
    event: Mapping[str, Any],
    verdict: Mapping[str, Any],
    symbols: Sequence[str],
) -> tuple[tuple[str, ...], bool]:
    """Derive bounded exact catalogue keys and whether issuer identity is safe enough for deletion.

    A non-numeric ticker is already an exchange-style identifier. A bare number is not: it becomes deletion-safe
    only when the source or judged headline carries an explicit market suffix such as ``02605.HK``.
    """

    texts = (
        str(event.get("leader_title") or ""),
        str(verdict.get("headline_zh") or ""),
        str(verdict.get("title_zh") or ""),
    )
    candidates: list[str] = []

    def add(value: object) -> None:
        normalized = str(value or "").strip().upper().replace(" ", "")
        if normalized and _SYMBOL_RE.fullmatch(normalized) and normalized not in candidates:
            candidates.append(normalized)

    confident = False
    requested = [str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()]
    for symbol in requested:
        add(symbol)
        if not symbol.isdigit():
            confident = True
    for text in texts:
        for match in _HK_CODE_RE.finditer(text):
            raw = match.group(0).upper()
            code = match.group("code").lstrip("0") or "0"
            padded = code.zfill(5)
            for value in (raw, code, padded, f"HK{code}", f"HK{padded}", f"{code}.HK", f"{padded}.HK"):
                add(value)
            confident = True
        company = _ASCII_COMPANY_RE.search(text)
        if company is not None:
            add(company.group("name").replace("&", "AND").replace("-", ""))
    return tuple(candidates[:24]), confident


__all__ = [
    "REQUIRED_TRADABILITY_VENUES",
    "TRADABILITY_REVIEW_TIMEOUT_SECONDS",
    "TradabilityMatch",
    "TradabilityReview",
    "TradabilityVerifier",
    "tradability_candidates",
]
