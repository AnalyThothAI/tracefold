"""Code-owned, content-addressed entry plans for one Trading Case."""

from __future__ import annotations

import hashlib
import json
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from itertools import pairwise
from typing import Any, Literal

from pydantic import Field, model_validator

from .contracts import Action, ExitPlan, Frozen
from .features import catalyst_text_values
from .policy import InvalidAssessment, is_citable_evidence

BAR_MS = 60_000
STRATEGY_VERSION = "entry_plan_v1"
ENTRY_WINDOW_MS = 120_000
MAX_HOLDING_SECONDS = 14_400


def _identity(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode()
    ).hexdigest()


def directed_cross(*, side: Literal["long", "short"], previous: Decimal, current: Decimal, level: Decimal) -> bool:
    """One crossing rule shared by plan, watcher and storage validation."""
    if not all(value.is_finite() and value > 0 for value in (previous, current, level)):
        return False
    return previous <= level < current if side == "long" else previous >= level > current


class EntryPlan(Frozen):
    plan_version: Literal["entry_plan_v1"] = "entry_plan_v1"
    plan_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    kind: Literal["immediate_entry_v1", "closed_bar_cross_v1"]
    asset_id: str
    instrument_semantics_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    side: Literal["long", "short"]
    exit_plan: ExitPlan
    required_evidence_refs: tuple[str, ...]
    source_revision: str
    source_first_visible_at_ms: int = Field(gt=0)
    reference_price: Decimal = Field(gt=0)
    reference_at_ms: int = Field(gt=0)
    expires_at_ms: int = Field(gt=0)
    level: Decimal | None = Field(default=None, gt=0)
    previous_close: Decimal | None = Field(default=None, gt=0)
    parent_plan_id: str | None = None

    @model_validator(mode="after")
    def check_kind(self) -> EntryPlan:
        if self.expires_at_ms <= self.reference_at_ms:
            raise ValueError("entry_plan_expiry_invalid")
        if self.kind == "closed_bar_cross_v1":
            if self.level is None or self.previous_close is None or self.parent_plan_id is not None:
                raise ValueError("cross_plan_condition_invalid")
        elif self.level is not None or self.previous_close is not None:
            raise ValueError("immediate_plan_condition_invalid")
        return self


def _exit_plan(rows: tuple[dict[str, Any], ...]) -> ExitPlan:
    if len(rows) < 16:
        raise ValueError("strategy_closed_bar_history_incomplete")
    rows = rows[-16:]
    stamps = [int(row["event_at_ms"]) for row in rows]
    if any(right - left != BAR_MS for left, right in pairwise(stamps)):
        raise ValueError("strategy_closed_bar_gap")
    try:
        prices = [(Decimal(str(row["high"])), Decimal(str(row["low"])), Decimal(str(row["close"]))) for row in rows]
    except (KeyError, TypeError, InvalidOperation) as exc:
        raise ValueError("strategy_price_invalid") from exc
    if any(not all(value.is_finite() and value > 0 for value in row) or row[0] < row[1] for row in prices):
        raise ValueError("strategy_price_invalid")
    baseline = prices[:-1]
    true_ranges = [
        max(high - low, abs(high - baseline[index - 1][2]), abs(low - baseline[index - 1][2]))
        for index, (high, low, _) in enumerate(baseline)
        if index > 0
    ]
    atr14 = sum(true_ranges, Decimal(0)) / Decimal(14)
    stop_bps = min(
        1_000,
        max(100, int((Decimal(2) * atr14 / prices[-1][2] * 10_000).to_integral_value(rounding=ROUND_CEILING))),
    )
    return ExitPlan(
        stop_distance_bps=stop_bps,
        take_profit_bps=2 * stop_bps,
        max_holding_seconds=MAX_HOLDING_SECONDS,
    )


def build_entry_plans(
    *,
    asset_id: str,
    instrument_semantics_digest: str,
    source_revision: str,
    source_fact: dict[str, Any],
    source_first_visible_at_ms: int,
    root_expires_at_ms: int,
    perp_rows: tuple[dict[str, Any], ...],
    parent_condition: dict[str, Any] | None = None,
    price_ref: str = "market:perp_bars",
) -> tuple[EntryPlan, ...]:
    """Return only plans supported by complete required inputs; missing ATR is no plan."""
    if source_fact.get("kind") not in ("oi", "catalyst") or source_first_visible_at_ms <= 0:
        return ()
    if source_fact["kind"] == "oi" and not all(
        source_fact.get(key) is not None for key in ("oi_change_bps", "measurement_definition")
    ):
        return ()
    if source_fact["kind"] == "catalyst" and not catalyst_text_values(source_fact):
        return ()
    if not perp_rows:
        return ()
    try:
        exit_plan = _exit_plan(perp_rows)
    except (ValueError, KeyError, TypeError):
        return ()
    rows = perp_rows[-16:]
    reference_price = Decimal(str(rows[-1]["close"]))
    reference_at_ms = int(rows[-1]["event_at_ms"])
    expires_at_ms = min(root_expires_at_ms, reference_at_ms + ENTRY_WINDOW_MS)
    if expires_at_ms <= reference_at_ms:
        return ()
    parent_plan_id = None if parent_condition is None else parent_condition.get("plan_id")
    sides: tuple[Literal["long", "short"], ...] = (
        ("long", "short") if parent_condition is None else (parent_condition["side"],)
    )
    base = {
        "asset_id": asset_id,
        "instrument_semantics_digest": instrument_semantics_digest,
        "source_revision": source_revision,
        "source_first_visible_at_ms": source_first_visible_at_ms,
        "reference_price": reference_price,
        "reference_at_ms": reference_at_ms,
        "exit_plan": exit_plan.model_dump(mode="json"),
        "required_evidence_refs": ("source", price_ref),
        "parent_plan_id": parent_plan_id,
        "bar_input": [(row["event_at_ms"], row["high"], row["low"], row["close"]) for row in rows],
    }
    plans: list[EntryPlan] = [
        EntryPlan(
            plan_id=_identity({**base, "kind": "immediate_entry_v1", "side": side}),
            kind="immediate_entry_v1",
            asset_id=asset_id,
            instrument_semantics_digest=instrument_semantics_digest,
            side=side,
            exit_plan=exit_plan,
            required_evidence_refs=("source", price_ref),
            source_revision=source_revision,
            source_first_visible_at_ms=source_first_visible_at_ms,
            reference_price=reference_price,
            reference_at_ms=reference_at_ms,
            expires_at_ms=expires_at_ms,
            parent_plan_id=parent_plan_id,
        )
        for side in sides
    ]
    if parent_condition is None:
        baseline = rows[:-1]
        upper = max(Decimal(str(row["high"])) for row in baseline)
        lower = min(Decimal(str(row["low"])) for row in baseline)
        previous = Decimal(str(baseline[-1]["close"]))
        if 0 < lower < upper:
            levels: tuple[tuple[Literal["long", "short"], Decimal], ...] = (("long", upper), ("short", lower))
            for side, level in levels:
                if directed_cross(side=side, previous=previous, current=reference_price, level=level):
                    continue
                plans.append(
                    EntryPlan(
                        plan_id=_identity({**base, "kind": "closed_bar_cross_v1", "side": side, "level": level}),
                        kind="closed_bar_cross_v1",
                        asset_id=asset_id,
                        instrument_semantics_digest=instrument_semantics_digest,
                        side=side,
                        exit_plan=exit_plan,
                        required_evidence_refs=("source", price_ref),
                        source_revision=source_revision,
                        source_first_visible_at_ms=source_first_visible_at_ms,
                        reference_price=reference_price,
                        reference_at_ms=reference_at_ms,
                        expires_at_ms=root_expires_at_ms,
                        level=level,
                        previous_close=previous,
                    )
                )
    return tuple(plans)


class AnalysisProposal(Frozen):
    assessment_version: Literal["trade_assessment_v4"] = "trade_assessment_v4"
    action: Action
    selected_plan_id: str | None = None
    supporting_evidence: tuple[str, ...] = ()
    opposing_evidence: tuple[str, ...] = ()
    judgment_refs: tuple[str, ...] = ()
    limitations: str | None = Field(default=None, max_length=2_000)
    public_rationale: str = Field(min_length=1, max_length=2_000)

    @model_validator(mode="after")
    def check_selection(self) -> AnalysisProposal:
        if (self.action == "NO_TRADE") != (self.selected_plan_id is None):
            raise ValueError("proposal_plan_selection_invalid")
        return self


class DirectedWatchCondition(Frozen):
    kind: Literal["closed_1m_directed_cross"] = "closed_1m_directed_cross"
    plan_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    side: Literal["long", "short"]
    level: Decimal = Field(gt=0)
    previous_close: Decimal = Field(gt=0)
    exit_plan: ExitPlan
    source_first_visible_at_ms: int
    frozen_at_ms: int
    expires_at_ms: int
    unit: Literal["USDT/base_asset"] = "USDT/base_asset"

    @model_validator(mode="after")
    def check_clock(self) -> DirectedWatchCondition:
        if self.expires_at_ms <= self.frozen_at_ms:
            raise ValueError("watch_expiry_invalid")
        return self


class PlanDecision(Frozen):
    decision_version: Literal["trade_decision_v4"] = "trade_decision_v4"
    action: Action
    selected_plan_id: str | None
    side: Literal["long", "short"] | None
    exit_plan: ExitPlan | None
    reason: str
    reason_code: str
    evidence_refs: tuple[str, ...]
    judgment_refs: tuple[str, ...] = ()
    limitations: str | None = None
    watch_condition: DirectedWatchCondition | None = None


def compile_proposal(
    *,
    proposal: AnalysisProposal,
    plans: tuple[EntryPlan, ...],
    evidence_catalog: dict[str, dict[str, Any]],
    judgment_refs: frozenset[str],
    now_ms: int,
) -> PlanDecision:
    """Compile only a visible plan with actual citable inputs and current scope."""
    menu = {plan.plan_id: plan for plan in plans}
    if len(menu) != len(plans):
        raise InvalidAssessment("plan_menu_invalid")
    citations = set(proposal.supporting_evidence + proposal.opposing_evidence)
    if citations - evidence_catalog.keys():
        raise InvalidAssessment("proposal_evidence_ref_unknown")
    if any(not is_citable_evidence(evidence_catalog[ref]) for ref in citations):
        raise InvalidAssessment("proposal_evidence_unavailable")
    if set(proposal.judgment_refs) - judgment_refs:
        raise InvalidAssessment("proposal_judgment_ref_unknown")
    selected = menu.get(proposal.selected_plan_id or "")
    if proposal.action != "NO_TRADE" and selected is None:
        raise InvalidAssessment("proposal_plan_outside_menu")
    if selected is not None:
        if any(not is_citable_evidence(evidence_catalog.get(ref) or {}) for ref in selected.required_evidence_refs):
            raise InvalidAssessment("proposal_required_evidence_unavailable")
        if now_ms >= selected.expires_at_ms:
            raise InvalidAssessment("proposal_plan_expired")
        if proposal.action == "TRADE" and selected.kind != "immediate_entry_v1":
            raise InvalidAssessment("proposal_trade_plan_kind_invalid")
        if proposal.action == "WATCH" and selected.kind != "closed_bar_cross_v1":
            raise InvalidAssessment("proposal_watch_plan_kind_invalid")
    watch = None
    if proposal.action == "WATCH" and selected is not None:
        if selected.level is None:
            raise InvalidAssessment("proposal_watch_plan_condition_invalid")
        watch = DirectedWatchCondition(
            plan_id=selected.plan_id,
            side=selected.side,
            level=selected.level,
            previous_close=selected.reference_price,
            exit_plan=selected.exit_plan,
            source_first_visible_at_ms=selected.source_first_visible_at_ms,
            frozen_at_ms=selected.reference_at_ms,
            expires_at_ms=selected.expires_at_ms,
        )
    return PlanDecision(
        action=proposal.action,
        selected_plan_id=None if selected is None else selected.plan_id,
        side=None if selected is None else selected.side,
        exit_plan=selected.exit_plan if proposal.action == "TRADE" and selected is not None else None,
        reason=proposal.public_rationale,
        reason_code=(
            "model_no_trade"
            if proposal.action == "NO_TRADE"
            else "model_watch"
            if proposal.action == "WATCH"
            else "immediate_entry"
        ),
        evidence_refs=tuple(sorted(citations)),
        judgment_refs=proposal.judgment_refs,
        limitations=proposal.limitations,
        watch_condition=watch,
    )
