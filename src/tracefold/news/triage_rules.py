"""decide(): deterministic post-rules over the Triage verdict (pure, golden-tested)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .models import Decision, TriageVerdict

PUSH_WINDOW_MS = 2 * 60 * 60_000
ESCALATE_WINDOW_MS = 4 * 60 * 60_000
_DIRECTIONAL = frozenset({"bullish", "bearish"})


@dataclass(frozen=True, slots=True)
class GateFacts:
    grounded_assets: tuple[str, ...]
    watchlist_symbols: frozenset[str]
    provider_score: float | None
    priority: str  # high | normal
    admission: str


@dataclass(frozen=True, slots=True)
class StorylineStatus:
    """Window facts computed by SQL for the event's storyline key (see repository.event_status)."""

    key: str
    pushed_2h: int = 0
    pushed_4h: int = 0
    max_magnitude_2h: int = 0
    max_magnitude_4h: int = 0
    directions_2h: tuple[str, ...] = ()
    directions_4h: tuple[str, ...] = ()
    last_push_ago_ms: int | None = None


@dataclass(frozen=True, slots=True)
class DecisionResult:
    final: Decision
    override_rule: str | None
    throttled_by: str | None
    rule_baseline: Decision
    watchlist_hits: tuple[str, ...] = field(default_factory=tuple)


def _base(symbol: str) -> str:
    return symbol.upper().replace("XYZ-", "")


def rule_baseline(facts: GateFacts) -> Decision:
    """The decision a pure-rule system would take with no model at all (fail-closed)."""

    watch = any(_base(s) in facts.watchlist_symbols for s in facts.grounded_assets)
    score = float(facts.provider_score or 0)
    if watch or (score >= 90 and facts.grounded_assets):
        return "push"
    return "drop"


def _direction_flip(direction: str, seen: Sequence[str]) -> bool:
    if direction not in _DIRECTIONAL:
        return False
    opposite = "bullish" if direction == "bearish" else "bearish"
    return opposite in seen and direction not in seen


def decide(
    verdict: TriageVerdict,
    facts: GateFacts,
    status: StorylineStatus | None,
    *,
    hourly_cap_reached: bool = False,
    muted: bool = False,
) -> DecisionResult:
    baseline = rule_baseline(facts)
    primaries = {_base(a.symbol) for a in verdict.assets if a.role == "primary"}
    grounded = {_base(s) for s in facts.grounded_assets}
    watch_hits = tuple(sorted(s for s in (primaries & grounded) if s in facts.watchlist_symbols))

    if muted:
        return DecisionResult("drop", "muted", None, baseline, watch_hits)
    if verdict.event_type == "noise":
        return DecisionResult("drop", "noise", None, baseline, watch_hits)

    final: Decision
    rule: str | None = None
    if verdict.magnitude == 3 and (verdict.direction in _DIRECTIONAL or verdict.scope == "macro"):
        final, rule = "escalate", "magnitude3"
    elif facts.priority == "high" and verdict.decision == "push":
        final, rule = "escalate", "high_priority_push"
    elif verdict.direction == "unclear":
        final, rule = "drop", "unclear_direction"
    elif verdict.magnitude >= 2 and verdict.actionable:
        final, rule = "push", "magnitude2_actionable"
    elif watch_hits and verdict.magnitude >= 1:
        final, rule = "push", "watchlist"
    else:
        final, rule = "drop", "below_threshold"

    if final in {"push", "escalate"} and status is not None:
        window_max = status.max_magnitude_2h if final == "push" else status.max_magnitude_4h
        pushed = status.pushed_2h if final == "push" else status.pushed_4h
        seen = status.directions_2h if final == "push" else status.directions_4h
        if pushed > 0 and verdict.magnitude <= window_max and not _direction_flip(verdict.direction, seen):
            return DecisionResult("throttled", rule, f"storyline:{status.key}", baseline, watch_hits)

    if final == "push" and hourly_cap_reached:
        return DecisionResult("throttled", rule, "hourly_cap", baseline, watch_hits)
    return DecisionResult(final, rule, None, baseline, watch_hits)


def fallback_verdict(facts: GateFacts, *, error_code: str) -> tuple[TriageVerdict, DecisionResult]:
    """Fail-closed degraded verdict when the model is unavailable."""

    baseline = rule_baseline(facts)
    verdict = TriageVerdict(
        event_type="noise" if baseline == "drop" else "macro",
        assets=[],
        direction="unclear",
        scope="macro",
        magnitude=0 if baseline == "drop" else 2,
        actionable=baseline == "push",
        confidence=0.0,
        decision=baseline,
        headline_zh="模型不可用（规则兜底）",
        rationale=error_code[:160],
    )
    return verdict, DecisionResult(baseline, "fail_closed_fallback", None, baseline)


def storyline_status_from_row(row: Mapping[str, Any] | None, key: str) -> StorylineStatus:
    if not row:
        return StorylineStatus(key=key)
    return StorylineStatus(
        key=key,
        pushed_2h=int(row.get("pushed_2h") or 0),
        pushed_4h=int(row.get("pushed_4h") or 0),
        max_magnitude_2h=int(row.get("max_magnitude_2h") or 0),
        max_magnitude_4h=int(row.get("max_magnitude_4h") or 0),
        directions_2h=tuple(row.get("directions_2h") or ()),
        directions_4h=tuple(row.get("directions_4h") or ()),
        last_push_ago_ms=(int(row["last_push_ago_ms"]) if row.get("last_push_ago_ms") is not None else None),
    )


__all__ = [
    "ESCALATE_WINDOW_MS",
    "PUSH_WINDOW_MS",
    "DecisionResult",
    "GateFacts",
    "StorylineStatus",
    "decide",
    "fallback_verdict",
    "rule_baseline",
    "storyline_status_from_row",
]
