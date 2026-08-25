"""Point-in-time liquidation event-study outcomes and deterministic cohort summaries."""

from __future__ import annotations

import hashlib
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

from ..contracts import Bar, PolicyDecision
from ..decision.regime import select_bar

EVENT_STUDY_VERSION = "liquidation_event_study_v2"
BAR_INTERVAL_MS = 300_000
HORIZONS_MS: tuple[tuple[str, int], ...] = (
    ("5s", 5_000),
    ("30s", 30_000),
    ("1m", 60_000),
    ("5m", 300_000),
    ("15m", 900_000),
    ("1h", 3_600_000),
)


@dataclass(frozen=True, slots=True)
class EventStudyPolicy:
    stop_bps: int
    take_profit_bps: int
    max_holding_ms: int
    taker_fee_bps_per_leg: int
    slippage_bps_per_leg: int = 2
    bar_interval_ms: int = BAR_INTERVAL_MS


def hypothesis_side(strategy_id: str, dominant_liquidated_side: str | None) -> Literal["long", "short"] | None:
    if dominant_liquidated_side not in {"long", "short"}:
        return None
    continuation: Literal["long", "short"] = "long" if dominant_liquidated_side == "short" else "short"
    return ("short" if continuation == "long" else "long") if "exhaustion" in strategy_id else continuation


def measure_event(
    bars: Sequence[Bar],
    *,
    cutoff_ms: int,
    decision: PolicyDecision,
    research_side: Literal["long", "short"] | None,
    policy: EventStudyPolicy,
    gap_tolerance_ms: int,
) -> dict[str, Any] | None:
    """Measure only closed bars at or after the cutoff; unsupported precision stays explicit."""

    ordered = sorted(bars, key=lambda bar: bar.close_at_ms)
    start = select_bar(ordered, target_ms=cutoff_ms, gap_tolerance_ms=gap_tolerance_ms)
    if start is None:
        return None
    horizons: dict[str, dict[str, Any]] = {}
    missing: list[str] = []
    for label, duration in HORIZONS_MS:
        if duration < policy.bar_interval_ms:
            horizons[label] = {
                "status": "missing",
                "reason": "source_bar_resolution_unsupported",
            }
            missing.append(f"horizon:{label}:source_bar_resolution_unsupported")
            continue
        end = select_bar(ordered, target_ms=cutoff_ms + duration, gap_tolerance_ms=gap_tolerance_ms)
        if end is None or end.close_at_ms <= start.close_at_ms:
            horizons[label] = {"status": "missing", "reason": "closed_bar_unavailable"}
            missing.append(f"horizon:{label}:closed_bar_unavailable")
            continue
        raw = _return_bps(start.close, end.close)
        horizons[label] = {
            "status": "measured",
            "return_bps": raw,
            "signed_return_bps": _signed(raw, research_side),
            "observed_at_ms": end.close_at_ms,
        }

    path = [bar for bar in ordered if start.close_at_ms < bar.close_at_ms <= cutoff_ms + 3_600_000]
    raw_path = [_return_bps(start.close, bar.close) for bar in path]
    signed_path = (
        [value if research_side == "long" else -value for value in raw_path] if research_side is not None else []
    )
    mfe = max(signed_path) if signed_path else None
    mae = min(signed_path) if signed_path else None
    exit_result = _simulate_exit(start.close, path, side=research_side, policy=policy, cutoff_ms=cutoff_ms)
    if exit_result.get("funding_cost_bps") is None:
        missing.append("cost:funding_unavailable")
    return {
        "schema": EVENT_STUDY_VERSION,
        "cutoff_ms": cutoff_ms,
        "start_price": str(start.close),
        "start_bar_closed_at_ms": start.close_at_ms,
        "source_bar_interval_ms": policy.bar_interval_ms,
        "strategy_decision": decision,
        "hypothesis_side": research_side,
        "horizons": horizons,
        "mfe_bps": mfe,
        "mae_bps": mae,
        "path_bar_count": len(path),
        "exit_simulation": exit_result,
        "missing_data": sorted(missing),
    }


def bootstrap_mean_interval(values: Sequence[int], *, cohort_key: str, samples: int = 1_000) -> dict[str, int] | None:
    """Stable non-parametric 95% interval; the cohort key fixes the resampling seed."""

    if not values:
        return None
    seed = int.from_bytes(hashlib.sha256(cohort_key.encode()).digest()[:8], "big")
    rng = random.Random(seed)  # noqa: S311 - deterministic statistical resampling, not security
    n = len(values)
    means = sorted(round(sum(values[rng.randrange(n)] for _ in range(n)) / n) for _ in range(samples))
    return {
        "mean_bps": round(sum(values) / n),
        "lower_95_bps": means[int(samples * 0.025)],
        "upper_95_bps": means[min(samples - 1, int(samples * 0.975))],
    }


def measured_horizon(outcome: Mapping[str, Any], label: str) -> int | None:
    horizons = outcome.get("horizons")
    if not isinstance(horizons, Mapping):
        return None
    value = horizons.get(label)
    if not isinstance(value, Mapping) or value.get("status") != "measured":
        return None
    result = value.get("signed_return_bps")
    return int(result) if isinstance(result, int) else None


def summarize_evaluation_rows(
    rows: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Build separate strategy/venue/liquidity cohorts without merging hypotheses."""

    grouped: dict[tuple[str, str, str], list[Mapping[str, Any]]] = {}
    by_strategy: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        strategy = str(row.get("strategy_id") or "")
        manifest = row.get("manifest")
        manifest = manifest if isinstance(manifest, Mapping) else {}
        instrument = manifest.get("instrument")
        instrument = instrument if isinstance(instrument, Mapping) else {}
        contexts = manifest.get("contexts")
        contexts = contexts if isinstance(contexts, Mapping) else {}
        market = contexts.get("market")
        market = market if isinstance(market, Mapping) else {}
        venue = str(instrument.get("exchange_id") or "unknown")
        bucket = _liquidity_bucket(market.get("depth_notional_usd"))
        grouped.setdefault((strategy, venue, bucket), []).append(row)
        by_strategy.setdefault(strategy, []).append(row)
    strategy_summary = {strategy: _summary(items, cohort_key=strategy) for strategy, items in by_strategy.items()}
    cohorts = []
    for (strategy, venue, bucket), items in sorted(grouped.items()):
        summary = _summary(items, cohort_key=f"{strategy}|{venue}|{bucket}")
        summary.update(
            {
                "cohort_key": f"{strategy}|{venue}|{bucket}",
                "strategy_id": strategy,
                "venue": venue,
                "liquidity_bucket": bucket,
            }
        )
        cohorts.append(summary)
    return strategy_summary, cohorts


def _summary(rows: Sequence[Mapping[str, Any]], *, cohort_key: str) -> dict[str, Any]:
    completed = [row for row in rows if isinstance(row.get("market_outcome"), Mapping)]
    horizons: dict[str, dict[str, Any]] = {}
    for label, _ in HORIZONS_MS:
        values = [value for row in completed if (value := measured_horizon(row["market_outcome"], label)) is not None]
        horizons[label] = {
            "measured": len(values),
            "missing": len(completed) - len(values),
            "bootstrap": bootstrap_mean_interval(values, cohort_key=f"{cohort_key}:{label}"),
        }
    mfe = _integer_values(completed, "mfe_bps")
    mae = _integer_values(completed, "mae_bps")
    net_ex_funding: list[int] = []
    exits: dict[str, int] = {}
    missing: dict[str, int] = {}
    source_complete = 0
    latency: list[int] = []
    for row in rows:
        manifest = row.get("manifest")
        manifest = manifest if isinstance(manifest, Mapping) else {}
        contexts = manifest.get("contexts")
        contexts = contexts if isinstance(contexts, Mapping) else {}
        fact = contexts.get("liquidation")
        fact = fact if isinstance(fact, Mapping) else {}
        contract = fact.get("source_contract")
        contract = contract if isinstance(contract, Mapping) else {}
        source_complete += int(contract.get("complete") is True)
        event_at, received_at = fact.get("event_at_ms"), fact.get("received_at_ms")
        if isinstance(event_at, int) and isinstance(received_at, int):
            latency.append(max(0, received_at - event_at))
    for row in completed:
        outcome = row["market_outcome"]
        for reason in outcome.get("missing_data", []):
            missing[str(reason)] = missing.get(str(reason), 0) + 1
        exit_result = outcome.get("exit_simulation")
        if isinstance(exit_result, Mapping):
            reason = str(exit_result.get("exit_reason") or exit_result.get("reason") or exit_result.get("status") or "")
            exits[reason] = exits.get(reason, 0) + 1
            value = exit_result.get("net_ex_funding_bps")
            if isinstance(value, int):
                net_ex_funding.append(value)
    promotion_reasons: list[str] = []
    # OpenNews gives us provider record identities, but not a durable upstream
    # duplicate/replay counter.  Keep that evidence gap visible instead of
    # manufacturing a zero duplicate rate from the rows that survived ingest.
    if rows:
        missing["source:duplicate_rate_unavailable"] = len(rows)
        promotion_reasons.append("duplicate_rate_unavailable")
    if source_complete != len(rows):
        promotion_reasons.append("source_contract_incomplete")
    if len(completed) < 100:
        promotion_reasons.append("holdout_sample_below_100")
    if any("funding" in reason for reason in missing):
        promotion_reasons.append("funding_cost_missing")
    if any(horizons[label]["missing"] for label in ("5s", "30s", "1m")):
        promotion_reasons.append("intraminute_coverage_missing")
    if len({str(row.get("underlying_key") or "") for row in rows}) < 3:
        promotion_reasons.append("symbol_diversity_below_3")
    return {
        "evaluated": len(rows),
        "completed": len(completed),
        "holdout": sum(int(row.get("research_partition") == "holdout") for row in rows),
        "source_contract_complete": source_complete,
        "coverage_bps": 0 if not rows else len(completed) * 10_000 // len(rows),
        "mean_source_latency_ms": None if not latency else round(sum(latency) / len(latency)),
        "duplicate_rate_bps": None,
        "horizons": horizons,
        "mfe_mean_bps": None if not mfe else round(sum(mfe) / len(mfe)),
        "mae_mean_bps": None if not mae else round(sum(mae) / len(mae)),
        "exit_by_reason": exits,
        "net_ex_funding_bootstrap": bootstrap_mean_interval(net_ex_funding, cohort_key=f"{cohort_key}:net_ex_funding"),
        "missing_data": missing,
        "promotion_ready": not promotion_reasons,
        "promotion_reasons": promotion_reasons,
        # Kept for the compact existing console row; it is the signed 1 h hypothesis return.
        "mean_return_bps": horizons["1h"]["bootstrap"]["mean_bps"] if horizons["1h"]["bootstrap"] else None,
    }


def _integer_values(rows: Sequence[Mapping[str, Any]], key: str) -> list[int]:
    return [value for row in rows if isinstance((value := row["market_outcome"].get(key)), int)]


def _liquidity_bucket(value: Any) -> str:
    if value is None:
        return "unknown"
    try:
        depth = Decimal(str(value))
    except Exception:
        return "unknown"
    if depth < Decimal("1000000"):
        return "lt_1m_usd"
    return "1m_to_10m_usd" if depth < Decimal("10000000") else "gte_10m_usd"


def _simulate_exit(
    entry: Decimal,
    path: Sequence[Bar],
    *,
    side: Literal["long", "short"] | None,
    policy: EventStudyPolicy,
    cutoff_ms: int,
) -> dict[str, Any]:
    assumptions = {
        "path_semantics": "closed_5m_trade_price_bars",
        "stop_bps": policy.stop_bps,
        "take_profit_bps": policy.take_profit_bps,
        "max_holding_ms": policy.max_holding_ms,
        "taker_fee_bps_per_leg": policy.taker_fee_bps_per_leg,
        "slippage_bps_per_leg": policy.slippage_bps_per_leg,
    }
    if side is None:
        return {**assumptions, "status": "not_applicable", "reason": "hypothesis_side_unavailable"}
    eligible = [bar for bar in path if bar.close_at_ms <= cutoff_ms + policy.max_holding_ms]
    if not eligible:
        return {**assumptions, "status": "missing", "reason": "holding_path_unavailable"}
    selected = eligible[-1]
    reason = "max_holding"
    for bar in eligible:
        signed = _signed(_return_bps(entry, bar.close), side)
        if signed is not None and signed <= -policy.stop_bps:
            selected, reason = bar, "stop_loss"
            break
        if policy.take_profit_bps > 0 and signed is not None and signed >= policy.take_profit_bps:
            selected, reason = bar, "take_profit"
            break
    gross = _signed(_return_bps(entry, selected.close), side)
    costs = 2 * (policy.taker_fee_bps_per_leg + policy.slippage_bps_per_leg)
    return {
        **assumptions,
        "status": "measured",
        "exit_reason": reason,
        "exit_at_ms": selected.close_at_ms,
        "exit_price": str(selected.close),
        "gross_return_bps": gross,
        "fee_bps": 2 * policy.taker_fee_bps_per_leg,
        "slippage_bps": 2 * policy.slippage_bps_per_leg,
        "funding_cost_bps": None,
        "net_ex_funding_bps": None if gross is None else gross - costs,
        "net_return_bps": None,
    }


def _return_bps(start: Decimal, end: Decimal) -> int:
    return int((end / start - 1) * 10_000)


def _signed(value: int, side: Literal["long", "short"] | None) -> int | None:
    if side is None:
        return None
    return value if side == "long" else -value


__all__ = [
    "BAR_INTERVAL_MS",
    "EVENT_STUDY_VERSION",
    "EventStudyPolicy",
    "bootstrap_mean_interval",
    "hypothesis_side",
    "measure_event",
    "measured_horizon",
    "summarize_evaluation_rows",
]
