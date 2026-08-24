"""Deterministic open-interest telemetry: parse, rank, and judge. No model, no prose.

OpenNews strategy 1019 (`OI Event Monitor`) pushes a fixed-format frame roughly 190 times a day::

    TRUMP OI Rise 4.55%, OI Value 32.17M, Whale Long Profit 80.21%, Whale/OI Ratio 100.71%

Those four numbers are the whole message. They need no language understanding and they carry no
storyline, so the Gate admits them as ``telemetry_deterministic`` and Triage judges them here instead
of spending two structured model calls re-reading numbers a regex already has.

What this module produces is an ordinary ``TriageVerdict``. That is the whole point: the rule the
reader asked for counts a symbol's eligible signals in a rolling window, and ``decide()`` is deliberately
unable to count — policy v7 removed every reader quota and ``StorylineStatus`` is tested to carry no
capacity field. Counting *here* and handing ``decide()`` a verdict it already understands keeps one
decision plane instead of two, and keeps delivery, receipts, outcome, feed, counters and audit on the
single path they were built for.

The symbol is the title's own leading token, normalized the way every other consumer of provider coin
tags normalizes one. Real tickers in this feed include single characters (``S``, ``4``), so the
template's capture is deliberately permissive about length and strict about position.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Final

from .models import TriageAsset, TriageVerdict

METRIC_VERSION: Final = "oi_signal_v1"
PARSER_VERSION: Final = "oi_signal_parser_v1"
RANK_SEMANTICS: Final = "eligible_rank_v1"
# The judge's identity on the verdict row, where a model-judged Event carries its ProgramArtifact sha.
# Content-addressed the same way: change the rule and the identity changes with it.
PROGRAM_VERSION: Final = "news_oi_signal_v1"
READER_CONTRACT_VERSION: Final = "oi_card_v2"
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
_INT64_MAX: Final = 2**63 - 1


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
    """Operator-owned thresholds for the OI lane, from `news.oi`; nothing is shared with `news.policy`.

    These are the numbers the volume rests on, so they are config rather than code: tuning them should
    not need a deploy, and a stored verdict carries the values it ran under in its trace.
    """

    window_ms: int = WINDOW_MS
    # The reader wants the opening qualifying moves of a run, not every tick of it.
    max_rank_in_window: int = 2
    # Whale exposure relative to total open interest. The name carries the inclusivity because the
    # rule is 大于 80%: a frame must *exceed* this, so exactly 8000 bps does not qualify. Measured over
    # 24 h of live frames the distribution is min 30% / p50 51% / p90 196%, so this removes about two
    # thirds before the eligible-rank ceiling is applied.
    whale_oi_ratio_above_bps: int = 8_000
    # A frame whose open interest barely moved is not a move; the name carries the inclusivity here
    # too. Zero disables the rule, which is the shipped default: the provider only emits a frame once
    # its own trigger fired.
    oi_change_at_least_bps: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "window_ms": self.window_ms,
            "max_rank_in_window": self.max_rank_in_window,
            "whale_oi_ratio_above_bps": self.whale_oi_ratio_above_bps,
            "oi_change_at_least_bps": self.oi_change_at_least_bps,
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
        direction="fall" if match.group("direction").lower() in _FALLING else "rise",
        oi_change_bps=change_bps,
        oi_value_usd=value_usd,
        whale_long_profit_bps=profit_bps,
        whale_oi_ratio_bps=ratio_bps,
    )


def oi_parse_failure(title: str, *, provider_source: str) -> tuple[TriageVerdict, dict[str, Any]]:
    """Fail-closed verdict and observable provider-contract trace for an unparseable 1019 frame."""

    title_sha256 = hashlib.sha256(title.encode("utf-8")).hexdigest()
    verdict = TriageVerdict(
        novelty="new_fact",
        event_type="noise",
        assets=[],
        direction="neutral",
        scope="single_name",
        magnitude=0,
        actionable=False,
        confidence=1.0,
        decision="drop",
        headline_zh=title[:60] or "持仓异动帧无法解析",
        why_zh="",
    )
    return verdict, {
        "parsed": False,
        "rule": "oi_parse_failed",
        "strategy_id": "1019",
        "provider": "opennews",
        "provider_source": provider_source,
        "title_sha256": title_sha256,
        "parser_version": PARSER_VERSION,
        "failure_stage": "template_match",
    }


def program_sha256(policy: OiPolicy = DEFAULT_OI_POLICY) -> str:
    """Content identity of this judge: the rule text plus the thresholds it ran under."""

    return hashlib.sha256(
        json.dumps(
            {
                "program": PROGRAM_VERSION,
                "reader_contract": READER_CONTRACT_VERSION,
                "metric": METRIC_VERSION,
                "parser": PARSER_VERSION,
                "rank_semantics": RANK_SEMANTICS,
                "policy": policy.as_dict(),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _headline(signal: OiSignal, *, rank: int, window_ms: int) -> str:
    """One complete deterministic reader title, with a bounded compact form for long tickers."""

    arrow = "▲" if signal.direction == "rise" else "▼"
    hours = max(1, int(window_ms) // 3_600_000)
    change = f"{_fixed_bps(abs(signal.oi_change_bps), places=2)}%"
    value = _usd_zh(signal.oi_value_usd).replace(" ", "")
    ratio = f"{_fixed_bps(signal.whale_oi_ratio_bps, places=1)}%"
    profit = f"{_fixed_bps(signal.whale_long_profit_bps, places=1)}%"
    rich = (
        f"{arrow} {signal.symbol} 持仓异动{change}｜持仓{value}｜鲸鱼占比{ratio}｜"
        f"鲸鱼多头盈利{profit}｜{hours}h内第{rank}次"
    )
    if len(rich) <= 60:
        return rich
    # TriageVerdict caps the reader headline at 60 characters. Preserve every measurement while shortening
    # labels only; the common short-symbol path above keeps the fully-spelled reader contract.
    compact = f"{arrow} {signal.symbol} OI{change}｜持仓{value}｜鲸鱼{ratio}｜盈利{profit}｜{hours}h#{rank}"
    if len(compact) <= 60:
        return compact
    # Provider fields are BIGINT-backed. A one-significant-digit scientific form therefore bounds every
    # numeric token while retaining the symbol, all four measurements, window and rank.
    bounded = (
        f"{arrow}{signal.symbol}Δ{_scientific(abs(signal.oi_change_bps), scale=2)}%/"
        f"O{_scientific(signal.oi_value_usd)}/W{_scientific(signal.whale_oi_ratio_bps, scale=2)}%/"
        f"P{_scientific(signal.whale_long_profit_bps, scale=2)}%/{_scientific(hours)}h#{_scientific(rank)}"
    )
    if len(bounded) > 60:  # Defensive against a direct caller bypassing the BIGINT/config contracts.
        raise ValueError("oi_headline_inputs_out_of_contract")
    return bounded


def _fixed_bps(value: int, *, places: int) -> str:
    divisor = 10 ** (2 - places)
    absolute = abs(int(value))
    units = (absolute + divisor // 2) // divisor
    base = 10**places
    whole, fraction = divmod(units, base)
    sign = "-" if value < 0 else ""
    return f"{sign}{whole}.{fraction:0{places}d}" if places else f"{sign}{whole}"


def _scientific(value: int, *, scale: int = 0) -> str:
    """One-significant-digit notation with a decimal scale; bounded for every PostgreSQL integer."""

    absolute = abs(int(value))
    if absolute == 0:
        return "0"
    if scale == 0 and absolute < 10_000:
        return str(int(value))
    digits = str(absolute)
    exponent = len(digits) - 1 - int(scale)
    leading = int(digits[0]) + (int(digits[1]) >= 5 if len(digits) > 1 else 0)
    if leading == 10:
        leading = 1
        exponent += 1
    sign = "-" if value < 0 else ""
    return f"{sign}{leading}e{exponent}"


def _usd_zh(value: int) -> str:
    """Compact USD for a reader, not for a ledger: 32_170_000 -> `3217 万`."""

    amount = int(value)
    if amount >= 100_000_000:
        hundredths = (amount * 100 + 50_000_000) // 100_000_000
        return f"{hundredths // 100}.{hundredths % 100:02d} 亿"
    if amount >= 10_000:
        return f"{amount / 10_000:.0f} 万"
    return str(amount)


def evaluate_oi(
    signal: OiSignal,
    *,
    earlier_eligible_count: int,
    policy: OiPolicy = DEFAULT_OI_POLICY,
) -> OiJudgment:
    """Judge one frame and express it as a ``TriageVerdict``.

    ``earlier_eligible_count`` is counted in PostgreSQL from this symbol's parsed rows inside the
    sliding window, using the same whale and OI-change thresholds as this judgment. Ineligible frames
    remain auditable but do not spend a later signal's rank.

    The verdict is shaped so ``decide()`` reaches the intended answer through its ordinary rules: a
    qualifying frame is an actionable, directional magnitude-2 ``oi_spike`` with a push intent, and a
    rejected one is a self-consistent magnitude-0 ``noise`` that policy v8's veto still holds.
    """

    rank = int(earlier_eligible_count) + 1
    if signal.whale_oi_ratio_bps <= policy.whale_oi_ratio_above_bps:
        rule = "whale_ratio_below_threshold"
    elif abs(signal.oi_change_bps) < policy.oi_change_at_least_bps:
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
        headline_zh=_headline(signal, rank=rank, window_ms=policy.window_ms),
        title_zh="",
        # All four deterministic measurements are already in the title. Repeating them as the body creates a
        # two-line card that reads like two separate claims; unlike model-judged News there is no causal why.
        why_zh="",
    )
    return OiJudgment(verdict=verdict, signal=signal, rank_in_window=rank, rule=rule)


def oi_judgment_trace(judgment: OiJudgment, *, policy: OiPolicy) -> dict[str, Any]:
    """Audit projection for one successfully parsed deterministic judgment."""

    signal = judgment.signal
    return {
        "parsed": True,
        "symbol": signal.symbol,
        "direction": signal.direction,
        "oi_change_bps": signal.oi_change_bps,
        "oi_value_usd": signal.oi_value_usd,
        "whale_long_profit_bps": signal.whale_long_profit_bps,
        "whale_oi_ratio_bps": signal.whale_oi_ratio_bps,
        "eligible_rank_in_window": judgment.rank_in_window,
        "rank_semantics": RANK_SEMANTICS,
        "rule": judgment.rule,
        "policy": policy.as_dict(),
    }


__all__ = [
    "DEFAULT_OI_POLICY",
    "METRIC_VERSION",
    "PARSER_VERSION",
    "PROGRAM_VERSION",
    "RANK_SEMANTICS",
    "READER_CONTRACT_VERSION",
    "WINDOW_MS",
    "OiJudgment",
    "OiPolicy",
    "OiSignal",
    "evaluate_oi",
    "oi_judgment_trace",
    "oi_parse_failure",
    "parse_oi_signal",
    "program_sha256",
]
