"""Post-delivery verification that a single-name ticker maps to a live tradeable contract."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
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
_PAREN_TICKER_RE = re.compile(
    r"(?P<name>[A-Za-z][A-Za-z0-9 .&-]{1,60}?)\s*[（(]\s*(?P<ticker>[A-Z][A-Z0-9]{0,5}(?:[.-][A-Z])?)\s*[）)]"
)
_EXCHANGE_TICKER_RE = re.compile(
    r"\b(?:NASDAQ|NYSE|NYSEARCA|AMEX|CBOE|LSE|HKEX|TSE|TSX|ASX)\s*[:：]\s*(?P<ticker>[A-Z][A-Z0-9.-]{0,9})\b",
    re.IGNORECASE,
)
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


@dataclass(frozen=True, slots=True)
class TradabilityCandidateIdentity:
    """Bounded catalogue keys plus separate search and destructive-action confidence."""

    candidates: tuple[str, ...]
    searchable: bool
    deletion_safe: bool


class TradabilityReview(ExactNewsModel):
    """Authoritative five-venue outcome used to choose edit, keep, or delete."""

    state: TradabilityState
    candidates: tuple[str, ...] = Field(max_length=24)
    checked_venues: tuple[TradabilityVenue, ...] = Field(max_length=5)
    failed_venues: tuple[TradabilityVenue, ...] = Field(max_length=5)
    matches: tuple[TradabilityMatch, ...] = Field(max_length=20)
    deletion_safe: bool = False
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
    """Derive bounded exact catalogue keys and whether they are specific enough to search."""

    identity = tradability_candidate_identity(event=event, verdict=verdict, symbols=symbols)
    return identity.candidates, identity.searchable


def tradability_candidate_identity(
    *,
    event: Mapping[str, Any],
    verdict: Mapping[str, Any],
    symbols: Sequence[str],
) -> TradabilityCandidateIdentity:
    """Separate catalogue-search confidence from permission to delete a delivered message.

    A non-numeric provider-grounded ticker is already an exchange-style identifier. When provider grounding is
    absent, the model's primary asset is a search seed only: an official catalogue match may promote it into the
    reader card, but its absence can never authorize deletion. A bare number is ambiguous unless the source or
    judged headline carries an explicit market suffix such as ``02605.HK``. A bare parenthesized acronym such as
    ``Apple (AAPL)`` is useful for a catalogue search, but cannot by itself authorize deletion because ordinary
    names such as ``OpenAI (GPT)`` have the same text shape.
    """

    texts = (
        str(event.get("leader_title") or ""),
        str(verdict.get("headline_zh") or ""),
    )
    candidates: list[str] = []

    def add(value: object) -> None:
        normalized = str(value or "").strip().upper().replace(" ", "")
        if normalized and _SYMBOL_RE.fullmatch(normalized) and normalized not in candidates:
            candidates.append(normalized)

    searchable = False
    deletion_safe = False
    requested = [str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()]
    for symbol in requested:
        add(symbol)
        if not symbol.isdigit():
            searchable = True
            deletion_safe = True
    if not requested:
        model_primaries = [
            str(asset.get("symbol") or "").strip().upper().removeprefix("XYZ-")
            for asset in (verdict.get("assets") or ())
            if isinstance(asset, Mapping) and asset.get("role") == "primary"
        ]
        for symbol in model_primaries:
            add(symbol)
            if symbol and not symbol.isdigit() and _SYMBOL_RE.fullmatch(symbol):
                searchable = True
    for text in texts:
        for match in _EXCHANGE_TICKER_RE.finditer(text):
            add(match.group("ticker"))
            searchable = True
            deletion_safe = True
        for match in _PAREN_TICKER_RE.finditer(text):
            add(match.group("ticker"))
            add(match.group("name").replace("&", "AND").replace("-", ""))
            searchable = True
        for match in _HK_CODE_RE.finditer(text):
            raw = match.group(0).upper()
            code = match.group("code").lstrip("0") or "0"
            padded = code.zfill(5)
            for value in (raw, code, padded, f"HK{code}", f"HK{padded}", f"{code}.HK", f"{padded}.HK"):
                add(value)
            searchable = True
            deletion_safe = True
        company = _ASCII_COMPANY_RE.search(text)
        if company is not None:
            add(company.group("name").replace("&", "AND").replace("-", ""))
    return TradabilityCandidateIdentity(tuple(candidates[:24]), searchable, deletion_safe)


__all__ = [
    "REQUIRED_TRADABILITY_VENUES",
    "TRADABILITY_REVIEW_TIMEOUT_SECONDS",
    "TradabilityCandidateIdentity",
    "TradabilityMatch",
    "TradabilityReview",
    "TradabilityVerifier",
    "tradability_candidate_identity",
    "tradability_candidates",
]
