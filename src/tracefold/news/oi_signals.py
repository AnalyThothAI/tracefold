"""Deterministic open-interest telemetry: parse, rank, and judge. No model, no prose.

OpenNews strategy 1019 (`OI Event Monitor`) pushes a fixed-format frame roughly 190 times a day::

    TRUMP OI Rise 4.55%, OI Value 32.17M, Whale Long Profit 80.21%, Whale/OI Ratio 100.71%

Those four numbers are the whole message. They need no language understanding and they carry no
storyline, so the Gate admits them as ``telemetry_deterministic`` and Triage judges them here instead
of spending two structured model calls re-reading numbers a regex already has.

What this module produces is an ordinary ``TriageVerdict``. That is the whole point: the rule the
reader asked for counts a symbol's frames in a rolling window, and ``decide()`` is deliberately
unable to count — policy v7 removed every reader quota and ``StorylineStatus`` is tested to carry no
capacity field. Counting *here* and handing ``decide()`` a verdict it already understands keeps one
decision plane instead of two, and keeps delivery, receipts, outcome, feed, counters and audit on the
single path they were built for.

The symbol comes from the provider's structured ``coins[].symbol`` where there is one, and the title's
leading token only as a fallback: real tickers in this feed include single characters (``S``, ``4``)
that no title regex should have to guess at.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from .models import TriageAsset, TriageVerdict

METRIC_VERSION: Final = "oi_signal_v1"
# The judge's identity on the verdict row, where a model-judged Event carries its ProgramArtifact sha.
# Content-addressed the same way: change the rule and the identity changes with it.
PROGRAM_VERSION: Final = "news_oi_signal_v1"
WINDOW_MS: Final = 4 * 3_600_000

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


def _base_symbol(symbol: str) -> str:
    """Provider tags carry an `XYZ-` prefix for the same instrument; strip it as the Gate does."""

    return str(symbol or "").strip().upper().removeprefix("XYZ-")


@dataclass(frozen=True, slots=True)
class OiSignal:
    """One parsed telemetry frame. Percentages are integer basis points, like `news_event_reactions`."""

    symbol: str
    direction: str
    oi_change_bps: int
    oi_value_usd: int
    whale_long_profit_bps: int
    whale_oi_ratio_bps: int


@dataclass(frozen=True, slots=True)
class OiPolicy:
    """Operator-owned thresholds for the OI lane; nothing here is shared with `news.policy`."""

    window_ms: int = WINDOW_MS
    # The reader wants the opening moves of a run, not every tick of it.
    max_rank_in_window: int = 2
    # Whale exposure relative to total open interest. Measured over 24 h of live frames the
    # distribution is min 30% / p50 51% / p90 196%, so this removes about two thirds; the rank
    # ceiling above is what does most of the filtering (128 -> 40 frames a day, together).
    min_whale_oi_ratio_bps: int = 8_000
    # A frame whose open interest barely moved is not a move. Zero disables the rule, which is the
    # shipped default: the provider only emits a frame once its own trigger fired.
    min_oi_change_bps: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "window_ms": self.window_ms,
            "max_rank_in_window": self.max_rank_in_window,
            "min_whale_oi_ratio_bps": self.min_whale_oi_ratio_bps,
            "min_oi_change_bps": self.min_oi_change_bps,
        }


DEFAULT_OI_POLICY: Final = OiPolicy()


@dataclass(frozen=True, slots=True)
class OiJudgment:
    """One deterministic judgment, in the vocabulary the rest of the pipeline already speaks."""

    verdict: TriageVerdict
    signal: OiSignal
    rank_in_window: int
    rule: str


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


def parse_oi_signal(title: str, coins: Sequence[Mapping[str, Any]] | None = None) -> OiSignal | None:
    """Parse one telemetry frame, or return None for anything that is not one.

    ``coins`` is the provider's structured tag list and wins over the title's leading token, which is
    only a fallback for a frame that arrived without metadata.
    """

    match = _TELEMETRY.match(str(title or ""))
    if match is None:
        return None
    # The provider ships one instrument under two tags (`UNITREE` and `XYZ-UNITREE`), and every other
    # consumer of coin tags strips the prefix. Without it the two spellings key two rolling windows and
    # the symbol gets twice its share of pushes, with `XYZ-` rendered into the card header.
    title_symbol = _base_symbol(match.group("symbol"))
    tagged = [_base_symbol(str(coin.get("symbol") or "")) for coin in coins or () if isinstance(coin, Mapping)]
    tagged = [candidate for candidate in tagged if candidate]
    # A multi-coin tag list must not key the row to an asset this frame is not about: prefer the tag the
    # title itself names, and fall back to the title when no tag matches it.
    symbol = next((candidate for candidate in tagged if candidate == title_symbol), "")
    symbol = symbol or (tagged[0] if len(tagged) == 1 else title_symbol)
    if not symbol:
        return None
    # Integer math here for the same reason as `_bps`: `int(float("8.29") * 10**6)` is 8_289_999.
    whole, _, frac = match.group("value").partition(".")
    unit = _UNIT[match.group("unit").upper()]
    scaled = int(whole or "0") * 1_000_000 + int((frac + "000000")[:6])
    value_usd = scaled * unit // 1_000_000
    return OiSignal(
        symbol=symbol,
        direction="fall" if match.group("direction").lower() in _FALLING else "rise",
        oi_change_bps=_bps(match.group("oi")),
        oi_value_usd=value_usd,
        whale_long_profit_bps=_bps(match.group("profit")),
        whale_oi_ratio_bps=_bps(match.group("ratio")),
    )


def program_sha256(policy: OiPolicy = DEFAULT_OI_POLICY) -> str:
    """Content identity of this judge: the rule text plus the thresholds it ran under."""

    return hashlib.sha256(
        json.dumps(
            {"program": PROGRAM_VERSION, "metric": METRIC_VERSION, "policy": policy.as_dict()},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _headline(signal: OiSignal) -> str:
    arrow = "▲" if signal.direction == "rise" else "▼"
    return f"{arrow} {signal.symbol} 持仓异动 {abs(signal.oi_change_bps) / 100.0:.2f}%"


def _why(signal: OiSignal, *, rank: int, window_ms: int) -> str:
    hours = max(1, int(window_ms) // 3_600_000)
    return " · ".join(
        (
            f"持仓 {_usd_zh(signal.oi_value_usd)}",
            f"鲸鱼占比 {signal.whale_oi_ratio_bps / 100.0:.1f}%",
            f"鲸鱼多头盈利 {signal.whale_long_profit_bps / 100.0:.1f}%",
            f"{hours}h 内第 {rank} 次",
        )
    )


def _usd_zh(value: int) -> str:
    """Compact USD for a reader, not for a ledger: 32_170_000 -> `3217 万`."""

    amount = int(value)
    if amount >= 100_000_000:
        return f"{amount / 100_000_000:.2f} 亿"
    if amount >= 10_000:
        return f"{amount / 10_000:.0f} 万"
    return str(amount)


def evaluate_oi(
    signal: OiSignal,
    *,
    earlier_at_ms: Sequence[int],
    now_ms: int,
    policy: OiPolicy = DEFAULT_OI_POLICY,
) -> OiJudgment:
    """Judge one frame and express it as a ``TriageVerdict``.

    ``earlier_at_ms`` is every frame already recorded for this symbol; only those inside the window
    count toward the rank, so the window slides with the reader rather than resetting on a clock
    boundary.

    Rank counts *frames*, not pushes: a symbol emitting a third frame has moved three times whether or
    not the earlier two cleared the whale threshold, and the reader asked for the opening moves of a
    run. The consequence is deliberate but worth knowing — a symbol churning out low-concentration
    frames spends its rank budget on them, so a later frame that does clear the threshold can arrive
    past the ceiling. The 40-pushes-a-day measurement was taken under exactly this rule.

    The verdict is shaped so ``decide()`` reaches the intended answer through its ordinary rules: a
    qualifying frame is an actionable, directional magnitude-2 ``oi_spike`` with a push intent, and a
    rejected one is a self-consistent magnitude-0 ``noise`` that policy v8's veto still holds.
    """

    cutoff = int(now_ms) - policy.window_ms
    rank = sum(1 for at in earlier_at_ms if at > cutoff) + 1
    if signal.whale_oi_ratio_bps <= policy.min_whale_oi_ratio_bps:
        rule = "whale_ratio_below_threshold"
    elif abs(signal.oi_change_bps) < policy.min_oi_change_bps:
        rule = "oi_change_below_threshold"
    elif rank > policy.max_rank_in_window:
        rule = "beyond_window_rank"
    else:
        rule = "opening_move_with_whale_concentration"
    qualifies = rule == "opening_move_with_whale_concentration"
    verdict = TriageVerdict(
        novelty="new_fact",
        restates=-1,
        # `oi_spike` has been in the taxonomy all along; until now it only ever landed on prose *about*
        # open interest, never on the telemetry itself.
        event_type="oi_spike" if qualifies else "noise",
        assets=[TriageAsset(symbol=signal.symbol, role="primary", market_type="perp")],
        direction="bullish" if signal.direction == "rise" else "bearish",
        scope="single_name",
        magnitude=2 if qualifies else 0,
        actionable=qualifies,
        # Not a probability: this judgment is arithmetic, and saying otherwise would put a fake number
        # into the same field a model fills with a real one.
        confidence=1.0,
        decision="push" if qualifies else "drop",
        audience="crypto",
        headline_zh=_headline(signal),
        title_zh="",
        why_zh=_why(signal, rank=rank, window_ms=policy.window_ms) if qualifies else "",
    )
    return OiJudgment(verdict=verdict, signal=signal, rank_in_window=rank, rule=rule)


__all__ = [
    "DEFAULT_OI_POLICY",
    "METRIC_VERSION",
    "PROGRAM_VERSION",
    "WINDOW_MS",
    "OiJudgment",
    "OiPolicy",
    "OiSignal",
    "evaluate_oi",
    "parse_oi_signal",
    "program_sha256",
]
