"""Deterministic open-interest telemetry: parse and present. No model, no prose, no push.

OpenNews strategy 1019 (`OI Event Monitor`) pushes a fixed-format frame roughly 190 times a day::

    TRUMP OI Rise 4.55%, OI Value 32.17M, Whale Long Profit 80.21%, Whale/OI Ratio 100.71%

Those four numbers are the whole message. They need no language understanding and they carry no
storyline, so the Gate admits them as ``telemetry_deterministic`` and this lane produces both the
reader presentation and its only ``DecisionResult`` without entering the model policy.

That ``DecisionResult`` is now always ``drop`` (#458). This lane used to run a second notification
rule of its own -- a whale-concentration threshold and an opening-rank ceiling -- beside Trading's
Alpha policy. Over 48 h the two picked disjoint sets: the reader was told about seven frames the
capital lane had refused, and none of the five it admitted. #459 then measured the provider's number
itself and found it is substantially price, not position: entered at a price a taker can actually
get, those frames returned -276 bps at 4 h against a +82 bps baseline. So the lane keeps parsing and
storing every frame -- the frame table, the Trading candidate feed and the audit trail are unchanged
-- and stops deciding that a human should be interrupted. The reader-facing card comes back as the
Signal card once #433-E powers on the Runtime.

The symbol is the title's own leading token, normalized the way every other consumer of provider coin
tags normalizes one. Real tickers in this feed include single characters (``S``, ``4``), so the
template's capture is deliberately permissive about length and strict about position.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any, Final

from .artifact_identity import canonical_sha
from .models import Decision, TriageAsset, TriageVerdict
from .oi_contracts import OI_METRIC_VERSION, OI_PARSE_FAILED_RULE, OI_STORED_RULE
from .program.contracts import JUDGMENT_CONTRACT_VERSION
from .source_contracts import OI_SOURCE_IDENTITY, SOURCE_CONTRACT_CLASSIFIER_VERSION, classify_source_contract
from .triage_rules import DecisionResult

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

# The judge's identity on the verdict row, where a model-judged Event carries its ProgramArtifact sha.
# Content-addressed the same way: change the rule and the identity changes with it. v3 is the rule that
# takes no action: no thresholds, no rank, one outcome.
PROGRAM_VERSION: Final = "news_oi_signal_v3"
# v4 drops the trailing `4h内第N次` clause with the window it counted in. The four measurements are
# unchanged, so a v3 card and a v4 card of the same frame still report the same numbers.
READER_CONTRACT_VERSION: Final = "oi_card_v4"

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
    direction: str
    oi_change_bps: int
    oi_value_usd: int
    whale_long_profit_bps: int
    whale_oi_ratio_bps: int


@dataclass(frozen=True, slots=True)
class OiJudgment:
    """One OI presentation, structured fact and action authority."""

    verdict: TriageVerdict
    signal: OiSignal | None
    rule: str
    decision: DecisionResult
    judgment_contract_version: str = field(default=JUDGMENT_CONTRACT_VERSION, init=False)

    @property
    def judgment_atom(self) -> dict[str, Any]:
        return {
            "judgment_contract_version": self.judgment_contract_version,
            "origin": "oi",
            "verdict": self.verdict.model_dump(mode="json"),
            "signal": None if self.signal is None else asdict(self.signal),
            "rule": self.rule,
            "decision": asdict(self.decision),
        }

    @property
    def judgment_sha256(self) -> str:
        return canonical_sha(self.judgment_atom)


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


def oi_parse_failure(title: str, *, provider_source: str) -> tuple[OiJudgment, dict[str, Any]]:
    """Issue a typed fail-closed judgment and observable trace for an invalid 1019 frame."""

    title_sha256 = hashlib.sha256(title.encode("utf-8")).hexdigest()
    verdict = TriageVerdict(
        novelty="new_fact",
        assets=[],
        direction="neutral",
        scope="single_name",
        magnitude=0,
        confidence=1.0,
        audience="crypto",
        headline_zh=title[:60] or "持仓异动帧无法解析",
        why_zh="",
    )
    rule = OI_PARSE_FAILED_RULE
    decision = DecisionResult(
        final="drop",
        override_rule=rule,
        throttled_by=None,
        rule_baseline="drop",
    )
    judgment = OiJudgment(verdict=verdict, signal=None, rule=rule, decision=decision)
    return judgment, {
        "parsed": False,
        "strategy_id": OI_SOURCE_IDENTITY.strategy_id,
        "provider": "opennews",
        "provider_source": provider_source,
        "title_sha256": title_sha256,
        "parser_version": PARSER_VERSION,
        "source_classifier_version": SOURCE_CONTRACT_CLASSIFIER_VERSION,
        "failure_stage": "source_contract_drift",
    }


def program_sha256() -> str:
    """Content identity of this judge.

    It takes no argument any more. Every operator-owned number this lane had was a notification
    threshold, and #458 removed the notification; what remains is fixed in code, so the identity is a
    constant that changes only with a deploy.
    """

    return hashlib.sha256(
        json.dumps(
            {
                "program": PROGRAM_VERSION,
                "reader_contract": READER_CONTRACT_VERSION,
                "judgment_contract": JUDGMENT_CONTRACT_VERSION,
                "metric": METRIC_VERSION,
                "parser": PARSER_VERSION,
                "source_contract": SOURCE_CONTRACT_VERSION,
                "source_classifier": SOURCE_CONTRACT_CLASSIFIER_VERSION,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _headline(signal: OiSignal) -> str:
    """One complete deterministic reader title, with a bounded compact form for long tickers.

    The four measurements and nothing else. The trailing `4h内第N次` clause left with the rank it
    counted (#458): it named a position in a push queue that no longer exists, and a card that keeps
    counting for a notification nobody sends is a claim about a rule the reader cannot check.
    """

    arrow = "▲" if signal.direction == "rise" else "▼"
    change = f"{_fixed_bps(abs(signal.oi_change_bps), places=2)}%"
    value = _usd_zh(signal.oi_value_usd).replace(" ", "")
    ratio = f"{_fixed_bps(signal.whale_oi_ratio_bps, places=1)}%"
    profit = f"{_fixed_bps(signal.whale_long_profit_bps, places=1)}%"
    rich = f"{arrow} {signal.symbol} 持仓异动{change}｜持仓{value}｜鲸鱼占比{ratio}｜鲸鱼多头盈利{profit}"
    if len(rich) <= 60:
        return rich
    # TriageVerdict caps the reader headline at 60 characters. Preserve every measurement while shortening
    # labels only; the common short-symbol path above keeps the fully-spelled reader contract.
    compact = f"{arrow} {signal.symbol} OI{change}｜持仓{value}｜鲸鱼{ratio}｜盈利{profit}"
    if len(compact) <= 60:
        return compact
    # Provider fields are BIGINT-backed. A one-significant-digit scientific form therefore bounds every
    # numeric token while retaining the symbol and all four measurements.
    bounded = (
        f"{arrow}{signal.symbol}Δ{_scientific(abs(signal.oi_change_bps), scale=2)}%/"
        f"O{_scientific(signal.oi_value_usd)}/W{_scientific(signal.whale_oi_ratio_bps, scale=2)}%/"
        f"P{_scientific(signal.whale_long_profit_bps, scale=2)}%"
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


def evaluate_oi(signal: OiSignal) -> OiJudgment:
    """Present one frame. Store it, tell nobody.

    Every frame gets the same answer, so there is no threshold to read and no rank to spend. The
    presentation is still complete and still directional -- the frame table renders this verdict, and
    Trading's candidate projection reads the same row -- but its magnitude is 0 and its decision is
    ``drop``, which is what "worth storing, not worth interrupting a human for" looks like in this
    lane's vocabulary.

    `stored` is deliberately not a withheld reason. `whale_ratio_below_threshold` and its siblings said
    "this frame was weighed and lost"; nothing is weighed here, and reusing a rejection name would put a
    judgment into the audit trail that no code performed.
    """

    verdict = TriageVerdict(
        novelty="new_fact",
        restates=-1,
        assets=[TriageAsset(symbol=signal.symbol, role="primary", market_type="perp")],
        direction="bullish" if signal.direction == "rise" else "bearish",
        scope="single_name",
        magnitude=0,
        # Not a probability: this judgment is arithmetic, and saying otherwise would put a fake number
        # into the same field a model fills with a real one.
        confidence=1.0,
        audience="crypto",
        headline_zh=_headline(signal),
        # All four deterministic measurements are already in the title. Repeating them as the body creates a
        # two-line card that reads like two separate claims; unlike model-judged News there is no causal why.
        why_zh="",
    )
    final: Decision = "drop"
    decision = DecisionResult(
        final=final,
        override_rule=OI_STORED_RULE,
        throttled_by=None,
        rule_baseline=final,
    )
    return OiJudgment(verdict=verdict, signal=signal, rule=OI_STORED_RULE, decision=decision)


def oi_judgment_trace(judgment: OiJudgment, *, source: OiSourceContract | None = None) -> dict[str, Any]:
    """Audit projection for one successfully parsed deterministic judgment.

    `source_window_unproven` is the stable reason for a frame whose measurement contract this judge
    could not establish. It is not a parse failure — the four numbers are perfectly good — and it does
    not change the reader's verdict; it says that no consumer may treat the frame as a claim about a
    particular interval.
    """

    signal = judgment.signal
    if signal is None:
        raise ValueError("oi_signal_missing_from_parsed_judgment")
    return {
        "parsed": True,
        "source_strategy_id": None if source is None else source.strategy_id,
        "source_contract_version": None if source is None else source.contract_version,
        "measurement_window_ms": None if source is None else source.measurement_window_ms,
        "source_contract_rule": "proven" if source is not None else "source_window_unproven",
        "parser_version": PARSER_VERSION,
        "source_classifier_version": SOURCE_CONTRACT_CLASSIFIER_VERSION,
    }


__all__ = [
    "METRIC_VERSION",
    "PARSER_VERSION",
    "PROGRAM_VERSION",
    "READER_CONTRACT_VERSION",
    "SOURCE_CONTRACT_VERSION",
    "OiJudgment",
    "OiSignal",
    "OiSourceContract",
    "evaluate_oi",
    "oi_judgment_trace",
    "oi_parse_failure",
    "oi_source_contract",
    "parse_oi_signal",
    "program_sha256",
]
