"""Deterministic open-interest telemetry: parse one frame into numbers. Nothing else.

OpenNews strategy 1019 (`OI Event Monitor`) pushes a fixed-format frame roughly 190 times a day::

    TRUMP OI Rise 4.55%, OI Value 32.17M, Whale Long Profit 80.21%, Whale/OI Ratio 100.71%

Those four numbers are the whole message. They need no language understanding and they carry no
storyline, so the frame is persisted at admission as a typed fact beside its Item and never enters
the editorial pipeline at all (#553).

What used to live here as well was a *judge*: a pseudo `TriageVerdict`, a reader headline, a
`DecisionResult` that was unconditionally `drop`, and a program identity for all of it. #458 had
already reduced the decision to one outcome, and #459 measured the provider's number itself and
found it is substantially price rather than position. A judgment with one possible answer, feeding a
card nobody is sent, is not a judgment; it was an editorial costume on a measurement. The
measurement stayed, and everything else has gone.

The symbol is the title's own leading token, normalized the way every other consumer of provider coin
tags normalizes one -- and kept unnormalized beside it, because the display short name is not the
instrument's identity. Real tickers in this feed include single characters (``S``, ``4``), so the
template's capture is deliberately permissive about length and strict about position.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from .oi_contracts import OI_METRIC_VERSION
from .source_contracts import SOURCE_CONTRACT_CLASSIFIER_VERSION, classify_source_contract

METRIC_VERSION: Final = OI_METRIC_VERSION
PARSER_VERSION: Final = "oi_signal_parser_v1"
# What this module claims to know about the provider's own measurement, as opposed to the four numbers
# it parses. Bumped when that measurement contract or a field's meaning changes.
SOURCE_CONTRACT_VERSION: Final = "opennews_oi_source_v1"
# The window a qualifying frame is measured over once the shared classifier has proven the exact
# provider identity in `news_items.provider_metadata.strategies[0]`.
#
# The title carries no interval — `TRUMP OI Rise 4.55%, OI Value 32.17M, …` says nothing about 5m — and
# there is no interval field anywhere in the provider payload, so the only two honest options are to
# read it from provider metadata (there is none) or to bind an exact strategy identity in code with a
# real fixture behind it (#265 §3.2). The shared source classifier owns that binding. Three things it must not
# become: a default when the identity is unknown, a value inferred from arrival-time deltas, or a
# constant inside a strategy with no provenance stored beside the frame.

# Why one Item with this template has no typed row. Not a rejection of the Item: the frame is stored
# and read either way, and this names which of the two it is.
RAW_REASON_TEMPLATE_UNMATCHED: Final = "oi_template_unmatched"

# Anchored on purpose: this must recognise the telemetry template and nothing that merely mentions
# open interest, such as "HIP-3 has lost $820M in open interest over the past 5 days".
_TELEMETRY = re.compile(
    r"^\s*(?P<symbol>\S{1,16})\s+OI\s+(?P<direction>Rise|Fall|Drop)\s+(?P<oi>-?\d+(?:\.\d+)?)\s*%,\s*"
    r"OI\s+Value\s+(?P<value>\d+(?:\.\d+)?)(?P<unit>[KMB]?),\s*"
    r"Whale\s+Long\s+Profit\s+(?P<profit>-?\d+(?:\.\d+)?)\s*%,\s*"
    r"Whale/OI\s+Ratio\s+(?P<ratio>-?\d+(?:\.\d+)?)\s*%\s*$",
    re.IGNORECASE,
)
_UNIT: Final[dict[str, int]] = {"": 1, "K": 10**3, "M": 10**6, "B": 10**9}
_FALLING: Final = frozenset({"fall", "drop"})
_INT64_MAX: Final = 2**63 - 1


def _base_symbol(symbol: str) -> str:
    """Provider tags carry an `XYZ-` prefix for the same instrument; strip it as the Gate does."""

    return str(symbol or "").strip().upper().removeprefix("XYZ-")


@dataclass(frozen=True, slots=True)
class OiSourceContract:
    """What the provider proves about *how* one frame was measured, beside what it measured.

    `whale_long_profit_bps` deserves its own sentence, because the name invites a stronger reading than
    the provider publishes. It is NewsLiquid's own `Whale Long Profit N%` percentage and nothing more:
    it is **not** "every smart-money account is in profit", **not** a total unrealised PnL in dollars,
    and **not** an account count. The provider publishes no `account_count`,
    `profitable_account_count`, `unrealized_pnl_usd` or `position_snapshot_at_ms`, so a consumer that
    renders any of those is inventing them. A future contract that does publish them gets a new
    version; this field's meaning may not change underneath a frozen Case.
    """

    strategy_id: str
    contract_version: str
    measurement_window_ms: int


def oi_source_contract(provider_metadata: Any) -> OiSourceContract | None:
    """The proven measurement contract for one frame, or `None` when it cannot be proven.

    `None` is a first-class answer and the caller records it as `source_window_unproven`. Returning a
    guess would put an unverified interval into an immutable Case and make every replay of it a claim
    about a window nobody checked.
    """

    strategies = provider_metadata.get("strategies") if isinstance(provider_metadata, Mapping) else None
    for strategy in strategies if isinstance(strategies, list | tuple) else ():
        if not isinstance(strategy, Mapping):
            continue
        view = {**provider_metadata, "strategies": [strategy]}
        contract = classify_source_contract(view)
        if contract.source_contract_family == "oi_v1":
            return OiSourceContract(
                strategy_id=contract.identity.strategy_id,
                contract_version=SOURCE_CONTRACT_VERSION,
                measurement_window_ms=300_000,
            )
    return None


@dataclass(frozen=True, slots=True)
class OiSignal:
    """One parsed telemetry frame. Percentages are integer basis points, like `news_event_reactions`."""

    symbol: str
    # The provider's own token before normalization, `XYZ-` prefix and all. Two spellings of one
    # instrument still key one measurement group through `symbol`; this is what the reader is shown
    # and what a consumer needs when the short name is ambiguous.
    raw_instrument: str
    direction: str
    oi_change_bps: int
    oi_value_usd: int
    whale_long_profit_bps: int
    whale_oi_ratio_bps: int


def measurement_definition(source: OiSourceContract | None) -> str:
    """The stable name of *what was measured*, for a reader grouping consecutive observations.

    Two frames belong in one group only when the same provider measured the same instrument on the
    same venue under the same definition. An unproven window is part of the definition, not a hole in
    it: frames whose interval nobody has established may not be merged with frames whose interval is
    known, so `unproven` is spelled out rather than left blank.
    """

    if source is None:
        return f"{OI_METRIC_VERSION}|unproven|unproven"
    return f"{OI_METRIC_VERSION}|{source.contract_version}|{source.measurement_window_ms}"


def _bps(value: str) -> int:
    """Percent string -> integer basis points, half-up, so 4.55% is exactly 455.

    Integer arithmetic on the decimal digits rather than a float: these numbers key a stored read
    model and a threshold comparison, and 0.1 + 0.2 has no business anywhere near either.
    """

    whole, _, frac = value.partition(".")
    sign = -1 if whole.strip().startswith("-") else 1
    # percent scaled by 10^4, so dividing by 100 lands on basis points with one rounding step.
    scaled = int(whole.strip().lstrip("+-") or "0") * 10_000 + int((frac + "0000")[:4])
    return sign * ((scaled + 50) // 100)


def parse_oi_signal(title: str) -> OiSignal | None:
    """Parse one telemetry frame, or return None for anything that is not one.

    Anything that is not the template returns ``None``: prose *about* open interest carries no numbers
    this rule can act on.
    """

    match = _TELEMETRY.match(str(title or ""))
    if match is None:
        return None
    # The title's leading token is the frame's own subject, so it decides. Preferring a provider tag
    # would key the row to an asset the frame is not about whenever the two disagree, and provider tags
    # are unbounded where `TriageAsset.symbol` is capped at 16 — a longer tag would raise inside the
    # verdict and dead-letter the message instead of dropping cleanly. The `XYZ-` prefix is stripped
    # because the provider ships one instrument under both spellings (`UNITREE`, `XYZ-UNITREE`) and
    # every other consumer of coin tags strips it; leaving it in would key two rolling windows for one
    # symbol and render `XYZ-` into the card header.
    symbol = _base_symbol(match.group("symbol"))
    if not symbol:
        return None
    # Integer math here for the same reason as `_bps`: `int(float("8.29") * 10**6)` is 8_289_999.
    try:
        whole, _, frac = match.group("value").partition(".")
        unit = _UNIT[match.group("unit").upper()]
        scaled = int(whole or "0") * 1_000_000 + int((frac + "000000")[:6])
        value_usd = scaled * unit // 1_000_000
        change_bps = _bps(match.group("oi"))
        profit_bps = _bps(match.group("profit"))
        ratio_bps = _bps(match.group("ratio"))
    except ValueError:
        return None
    # These four fields are persisted as PostgreSQL BIGINTs. Rejecting an out-of-contract provider frame is
    # safer than constructing a verdict that can only fail later inside the transaction.
    if value_usd > _INT64_MAX or any(abs(value) > _INT64_MAX for value in (change_bps, profit_bps, ratio_bps)):
        return None
    return OiSignal(
        symbol=symbol,
        raw_instrument=match.group("symbol").strip()[:32],
        direction="fall" if match.group("direction").lower() in _FALLING else "rise",
        oi_change_bps=change_bps,
        oi_value_usd=value_usd,
        whale_long_profit_bps=profit_bps,
        whale_oi_ratio_bps=ratio_bps,
    )


__all__ = [
    "METRIC_VERSION",
    "PARSER_VERSION",
    "RAW_REASON_TEMPLATE_UNMATCHED",
    "SOURCE_CONTRACT_CLASSIFIER_VERSION",
    "SOURCE_CONTRACT_VERSION",
    "OiSignal",
    "OiSourceContract",
    "measurement_definition",
    "oi_source_contract",
    "parse_oi_signal",
]
