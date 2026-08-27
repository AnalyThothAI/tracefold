"""Deterministic OpenNews liquidation facts. Parse at admission; never call a model."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Final, Literal

from .models import TriageAsset, TriageVerdict
from .source_contracts import LIQUIDATION_SOURCE_IDENTITY, SOURCE_CONTRACT_CLASSIFIER_VERSION
from .triage_rules import DecisionResult

PARSER_VERSION: Final = "liquidation_parser_v1"
PROGRAM_VERSION: Final = "news_liquidation_fact_v1"
READER_CONTRACT_VERSION: Final = "liquidation_card_v1"
ADMISSION_POLICY_VERSION: Final = "news_liquidation_admission_v1"
TRIAGE_POLICY_VERSION: Final = "news_liquidation_policy_v1"
SOURCE_CONTRACT_VERSION: Final = "opennews_liquidation_source_v1"

_FRAME = re.compile(
    r"^\s*(?P<symbol>[A-Z0-9._-]{1,16})\s+Large\s+"
    r"(?P<side>Short|Long)\s+Liquidation\s+"
    r"(?P<notional>\d+(?:\.\d+)?)(?P<unit>[KMB]?)\s+at\s+\$(?P<price>\d+(?:\.\d+)?)\s*$",
    re.IGNORECASE,
)
_UNIT: Final[dict[str, Decimal]] = {
    "": Decimal(1),
    "K": Decimal(1_000),
    "M": Decimal(1_000_000),
    "B": Decimal(1_000_000_000),
}
_VENUES: Final = frozenset({"binance", "hyperliquid"})
_MAX_NUMERIC: Final = Decimal("1e24")


@dataclass(frozen=True, slots=True)
class LiquidationFact:
    source_key: str
    item_id: str
    fact_id: str
    symbol: str
    venue: Literal["binance", "hyperliquid"]
    liquidated_position_side: Literal["long", "short"]
    forced_order_side: Literal["buy", "sell"]
    notional_usd: Decimal
    quantity: Decimal | None
    price: Decimal
    event_at_ms: int
    received_at_ms: int
    provider_record_identity: str
    symbol_contract_identity: str
    position_side_semantics: str
    quantity_semantics: str
    notional_semantics: str
    price_semantics: str
    completeness_assumption: str
    throttle_assumption: str
    source_contract_version: str = SOURCE_CONTRACT_VERSION
    source_contract_complete: bool = False
    parser_version: str = PARSER_VERSION


def source_key(*, item_id: str, fact_id: str) -> str:
    """Provider-record identity plus fact and parser generation."""

    return hashlib.sha256(f"{item_id}\x1f{fact_id}\x1f{PARSER_VERSION}".encode()).hexdigest()


def parse_liquidation(
    title: str,
    *,
    item_id: str,
    fact_id: str,
    provider_source: str,
    event_at_ms: int,
    received_at_ms: int,
    provider_record_identity: str | None = None,
) -> LiquidationFact | None:
    """Parse the Strategy 2000 wire template, failing closed on ambiguous units or venue."""

    match = _FRAME.fullmatch(str(title or ""))
    venue = str(provider_source or "").strip().lower()
    if match is None or venue not in _VENUES or event_at_ms <= 0 or received_at_ms < event_at_ms:
        return None
    try:
        notional = Decimal(match.group("notional")) * _UNIT[match.group("unit").upper()]
        price = Decimal(match.group("price"))
    except (InvalidOperation, KeyError):
        return None
    if notional <= 0 or price <= 0 or notional > _MAX_NUMERIC or price > _MAX_NUMERIC:
        return None
    position_side = match.group("side").lower()
    symbol = match.group("symbol").upper().removeprefix("XYZ-")
    if not symbol:
        return None
    return LiquidationFact(
        source_key=source_key(item_id=item_id, fact_id=fact_id),
        item_id=item_id,
        fact_id=fact_id,
        symbol=symbol,
        venue=venue,  # type: ignore[arg-type]
        liquidated_position_side=position_side,  # type: ignore[arg-type]
        forced_order_side="buy" if position_side == "short" else "sell",
        notional_usd=notional,
        quantity=None,
        price=price,
        event_at_ms=int(event_at_ms),
        received_at_ms=int(received_at_ms),
        provider_record_identity=str(provider_record_identity or item_id),
        # OpenNews names a base symbol and venue but not the exact listed contract.
        symbol_contract_identity=f"unresolved:{venue}:{symbol}",
        position_side_semantics="template_position_side;short=>forced_buy;long=>forced_sell",
        quantity_semantics="not_provided",
        notional_semantics="provider_reported_usd_notional",
        price_semantics="provider_reported_unspecified_price",
        completeness_assumption="selected_events_without_heartbeat_sequence_or_coverage_sla",
        throttle_assumption="provider_throttle_unknown",
    )


def parse_failure(title: str, *, provider_source: str) -> tuple[TriageVerdict, dict[str, Any]]:
    """Deterministic fail-closed verdict without retaining provider prose in the trace."""

    failure = {
        "parsed": False,
        "rule": "liquidation_parse_failed",
        "strategy_id": LIQUIDATION_SOURCE_IDENTITY.strategy_id,
        "provider": "opennews",
        "provider_source": str(provider_source or ""),
        "title_sha256": hashlib.sha256(str(title or "").encode()).hexdigest(),
        "parser_version": PARSER_VERSION,
        "source_classifier_version": SOURCE_CONTRACT_CLASSIFIER_VERSION,
        "failure_stage": "source_contract_drift",
    }
    return (
        TriageVerdict(
            novelty="new_fact",
            event_type="noise",
            assets=[],
            direction="neutral",
            scope="single_name",
            magnitude=0,
            actionable=False,
            confidence=1.0,
            decision="drop",
            audience="crypto",
            headline_zh=(str(title or "")[:60] or "强平遥测帧无法解析"),
        ),
        failure,
    )


def reader_decision(verdict: TriageVerdict, *, error_code: str | None = None) -> DecisionResult:
    """The independent liquidation reader policy; it never calls News editorial policy v10."""

    return DecisionResult(
        final=verdict.decision,
        override_rule=error_code or "liquidation_deterministic",
        throttled_by=None,
        rule_baseline=verdict.decision,
    )


def verdict(fact: LiquidationFact) -> TriageVerdict:
    """Reader fact only: forced flow is not a forecast and never chooses a trade side."""

    side = "空头" if fact.liquidated_position_side == "short" else "多头"
    headline = f"{fact.symbol} {side}强平 {_compact_usd(fact.notional_usd)}｜{fact.venue}"
    return TriageVerdict(
        novelty="new_fact",
        event_type="liquidation",
        assets=[TriageAsset(symbol=fact.symbol, role="primary", market_type="perp")],
        direction="neutral",
        scope="single_name",
        magnitude=2,
        actionable=False,
        confidence=1.0,
        decision="push",
        audience="crypto",
        headline_zh=headline[:60],
        why_zh="已发生的被迫成交，不代表后续方向；仅进入影子策略研究。",
    )


def trace(fact: LiquidationFact) -> dict[str, Any]:
    return {
        "parsed": True,
        "source_key": fact.source_key,
        "symbol": fact.symbol,
        "venue": fact.venue,
        "liquidated_position_side": fact.liquidated_position_side,
        "forced_order_side": fact.forced_order_side,
        "notional_usd": str(fact.notional_usd),
        "quantity": None,
        "price": str(fact.price),
        "event_at_ms": fact.event_at_ms,
        "received_at_ms": fact.received_at_ms,
        "source_latency_ms": max(0, fact.received_at_ms - fact.event_at_ms),
        "parser_version": fact.parser_version,
        "source_classifier_version": SOURCE_CONTRACT_CLASSIFIER_VERSION,
        "source_contract": {
            "provider_record_identity": fact.provider_record_identity,
            "symbol_contract_identity": fact.symbol_contract_identity,
            "position_side_semantics": fact.position_side_semantics,
            "quantity_semantics": fact.quantity_semantics,
            "notional_semantics": fact.notional_semantics,
            "price_semantics": fact.price_semantics,
            "completeness_assumption": fact.completeness_assumption,
            "throttle_assumption": fact.throttle_assumption,
            "source_contract_version": fact.source_contract_version,
            "complete": fact.source_contract_complete,
        },
        "rule": "liquidation_fact_only",
    }


def program_sha256() -> str:
    return hashlib.sha256(
        json.dumps(
            {
                "program": PROGRAM_VERSION,
                "reader_contract": READER_CONTRACT_VERSION,
                "admission_policy": ADMISSION_POLICY_VERSION,
                "triage_policy": TRIAGE_POLICY_VERSION,
                "parser": PARSER_VERSION,
                "source_contract": SOURCE_CONTRACT_VERSION,
                "source_classifier": SOURCE_CONTRACT_CLASSIFIER_VERSION,
                "side_semantics": {"short": "buy", "long": "sell"},
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _compact_usd(value: Decimal) -> str:
    for unit, divisor in (("B", Decimal(1_000_000_000)), ("M", Decimal(1_000_000)), ("K", Decimal(1_000))):
        if value >= divisor:
            return f"${value / divisor:.2f}{unit}"
    return f"${value:.2f}"


__all__ = [
    "ADMISSION_POLICY_VERSION",
    "PARSER_VERSION",
    "PROGRAM_VERSION",
    "READER_CONTRACT_VERSION",
    "SOURCE_CONTRACT_VERSION",
    "TRIAGE_POLICY_VERSION",
    "LiquidationFact",
    "parse_failure",
    "parse_liquidation",
    "program_sha256",
    "reader_decision",
    "source_key",
    "trace",
    "verdict",
]
