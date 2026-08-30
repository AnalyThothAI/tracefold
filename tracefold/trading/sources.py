"""One projected OI row in, one typed Source out, or one named source-contract failure.

Pure functions over rows — no database, no clock, no network. The News package owns the read; this
module owns whether the row is a usable market fact at all. Keeping the rule here rather than in SQL
is deliberate: the admission ledger and the check must be the same code, or the ledger eventually
describes a filter the lane no longer applies.

It fails closed on everything it cannot prove. A symbol that canonicalises to nothing, a missing rank,
an unknown direction, a retired upstream generation — each is a named rejection, never a default.

**Age is not one of these rules.** `normalize_oi_source` answers "is this a usable fact", which is a
property of the row. Whether it is fresh enough to open a Case is Admission's, with its own budget.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from .bindings import binding_for_source_venue, venue_for_binding
from .contracts import (
    OiCandidateRow,
    OiTradeCandidate,
    canonical_base_symbol,
)


@dataclass(frozen=True, slots=True)
class SourceRejected:
    """A row that is not a usable OI fact, and the rule that says so."""

    rule: str
    symbol: str = ""


def _int(value: Any, default: int | None = None) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def normalize_oi_source(row: OiCandidateRow) -> OiTradeCandidate | SourceRejected:
    """One projected telemetry fact, or a named source-contract failure. No clock, no policy (#264).

    This is the **source** stage and nothing else: is the row a usable, live, current-generation OI
    fact at all? The liquidity floor, the deny list, freshness and idempotency belong to Admission,
    and they used to be here as well as in News's SELECT — which is how the same threshold came to be
    executed in three places and a rejection came to be indistinguishable from a row that never
    existed. (A rank ceiling and a per-symbol cooldown were on that list until #348 retired both.)

    `rank_in_window` is read here because the candidate carries it, not because a ceiling is applied.
    """

    symbol = canonical_base_symbol(row.get("symbol"))
    if not symbol:
        return SourceRejected(rule="symbol_not_canonicalisable")
    # The reader's push/drop is carried onto the candidate as audit and is not an admission (#264): its
    # rule is `whale_oi_ratio > 80%`, and gating capital on it meant a reader policy edit opened or
    # closed the trading lane without anyone deciding that it should.
    if str(row.get("ingest_mode") or "") != "live":
        return SourceRejected(rule="not_live_ingest", symbol=symbol)

    observed = _int(row.get("observed_at_ms"))
    if observed is None:
        return SourceRejected(rule="observed_at_missing", symbol=symbol)

    verdict_at = _int(row.get("verdict_created_at_ms"))
    if verdict_at is None:
        return SourceRejected(rule="verdict_time_missing", symbol=symbol)

    rank = _int(row.get("rank_in_window"))
    if rank is None:
        return SourceRejected(rule="rank_missing", symbol=symbol)

    measurements = {
        "oi_change_bps": _int(row.get("oi_change_bps")),
        "oi_value_usd": _int(row.get("oi_value_usd")),
        "whale_long_profit_bps": _int(row.get("whale_long_profit_bps")),
        "whale_oi_ratio_bps": _int(row.get("whale_oi_ratio_bps")),
    }
    missing_measurement = next((name for name, value in measurements.items() if value is None), None)
    if missing_measurement is not None:
        return SourceRejected(rule=f"{missing_measurement}_missing", symbol=symbol)

    direction = str(row.get("direction") or "").strip().lower()
    if direction not in ("rise", "fall"):
        return SourceRejected(rule="oi_direction_unknown", symbol=symbol)

    try:
        source_rule = row["source_rule"]
    except KeyError:
        return SourceRejected(rule="source_rule_missing", symbol=symbol)
    if not isinstance(source_rule, str) or not source_rule.strip():
        return SourceRejected(rule="source_rule_missing", symbol=symbol)

    venue = str(row.get("venue") or "").strip().lower()
    binding = binding_for_source_venue(venue)
    if binding is not None:
        venue = venue_for_binding(binding)
    try:
        return OiTradeCandidate(
            event_id=str(row.get("event_id") or ""),
            observed_at_ms=observed,
            verdict_created_at_ms=verdict_at,
            base_symbol=symbol,
            venue=venue,
            oi_direction=direction,
            oi_change_bps=measurements["oi_change_bps"],
            oi_value_usd=measurements["oi_value_usd"],
            whale_long_profit_bps=measurements["whale_long_profit_bps"],
            whale_oi_ratio_bps=measurements["whale_oi_ratio_bps"],
            rank_in_window=rank,
            final_decision=str(row.get("final_decision") or ""),
            source_rule=source_rule,
            metric_version=str(row.get("metric_version") or ""),
            # Carried, never defaulted. A frame whose measurement window the provider contract could
            # not prove reaches the policy as `None`, and the policy refuses it by name (#265).
            source_strategy_id=(str(row["source_strategy_id"]) if row.get("source_strategy_id") else None),
            source_contract_version=(
                str(row["source_contract_version"]) if row.get("source_contract_version") else None
            ),
            measurement_window_ms=_int(row.get("measurement_window_ms")),
            learning_epoch=str(row.get("learning_epoch") or ""),
            program_version=str(row.get("program_version") or ""),
            program_sha256=str(row.get("program_sha256") or ""),
            policy_version=str(row.get("policy_version") or ""),
            judgment_contract_version=str(row.get("judgment_contract_version") or ""),
            judgment_origin=str(row.get("judgment_origin") or ""),
            judgment_sha256=str(row.get("judgment_sha256") or ""),
            runtime_manifest_sha=str(row.get("runtime_manifest_sha") or ""),
        )
    except ValidationError:
        # The Program, policy and editorial origin are `Literal`s on the candidate, so a row from a
        # retired generation lands here rather than being frozen into a manifest claiming to be
        # current.
        return SourceRejected(rule="generation_invalid", symbol=symbol)


__all__ = ["SourceRejected", "normalize_oi_source"]
