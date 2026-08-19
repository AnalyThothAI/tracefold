"""decide(): deterministic post-rules over the Triage verdict (pure, golden-tested)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from .models import Decision, TriageVerdict

PUSH_WINDOW_MS = 2 * 60 * 60_000
ESCALATE_WINDOW_MS = 4 * 60 * 60_000
_DIRECTIONAL = frozenset({"bullish", "bearish"})
_MODEL_WANTS_PUSH = frozenset({"push", "escalate"})


_UNCLEAR_PUSH_EVENT_TYPES: Final = (
    "product",
    "listing",
    "delisting",
    "regulation",
    "hack",
    "exploit",
    "partnership",
    "filing",
)


@dataclass(frozen=True, slots=True)
class DecidePolicy:
    """Tunable thresholds of decide(); the defaults are the live policy (TRIAGE_POLICY_VERSION), operator-owned
    through ``news.policy``."""

    escalate_magnitude: int = 3
    min_push_magnitude: int = 1
    min_watchlist_magnitude: int = 1
    unclear_push_event_types: tuple[str, ...] = _UNCLEAR_PUSH_EVENT_TYPES
    unclear_push_min_magnitude: int = 2
    theme_cap_4h: int = 3
    storyline_throttle: bool = True
    hourly_cap_enabled: bool = True
    # Policy v3 (issue #61): the model's novelty verdict against the told ledger. A grounded restatement never
    # pushes; a novel event (new_fact / progression) at >= novel_min_magnitude may pass the storyline throttle up to a
    # hard cap (theme: pushes per 4 h; asset: pushes per 2 h). Defaults are the values measured in the #61 replay.
    restatement_drop: bool = True
    novel_min_magnitude: int = 2
    theme_hard_cap_4h: int = 6
    asset_hard_cap_2h: int = 3


DEFAULT_POLICY = DecidePolicy()


@dataclass(frozen=True, slots=True)
class GateFacts:
    grounded_assets: tuple[str, ...]
    watchlist_symbols: frozenset[str]
    provider_score: float | None
    priority: str  # high | normal
    admission: str


@dataclass(frozen=True, slots=True)
class StorylineStatus:
    """Window facts computed by SQL for the event's storyline key (see repository.event_status), plus the direction
    of every told-ledger entry the model saw (index = the ``i`` it cites in ``restates``; empty when no ledger)."""

    key: str
    pushed_2h: int = 0
    pushed_4h: int = 0
    max_magnitude_2h: int = 0
    max_magnitude_4h: int = 0
    directions_2h: tuple[str, ...] = ()
    directions_4h: tuple[str, ...] = ()
    last_push_ago_ms: int | None = None
    told_directions: tuple[str, ...] = ()

    @property
    def told_count(self) -> int:
        return len(self.told_directions)


@dataclass(frozen=True, slots=True)
class DecisionResult:
    final: Decision
    override_rule: str | None
    throttled_by: str | None
    rule_baseline: Decision
    watchlist_hits: tuple[str, ...] = field(default_factory=tuple)


def _base(symbol: str) -> str:
    return symbol.upper().replace("XYZ-", "")


_BASELINE_MIN_SCORE: Final = 80.0


def rule_baseline(facts: GateFacts) -> Decision:
    """The decision a pure-rule system would take with no model at all: watchlist, or a provider score >= 80 on a
    grounded asset, pushes; everything else drops (and is counted as degraded, never silently)."""

    watch = any(_base(s) in facts.watchlist_symbols for s in facts.grounded_assets)
    score = float(facts.provider_score or 0)
    if watch or (score >= _BASELINE_MIN_SCORE and facts.grounded_assets):
        return "push"
    return "drop"


def _direction_flip(direction: str, seen: Sequence[str]) -> bool:
    if direction not in _DIRECTIONAL:
        return False
    opposite = "bullish" if direction == "bearish" else "bearish"
    return opposite in seen and direction not in seen


def grounded_restatement(verdict: TriageVerdict, status: StorylineStatus | None) -> bool:
    """True when the model called this a restatement *of a ledger entry it was actually shown* and the direction did
    not flip against that entry. An out-of-range ``restates`` (or an empty ledger) is ignored: novelty then counts as
    new_fact, so a hallucinated restatement can never drop a card."""

    if verdict.novelty != "restatement" or status is None or status.told_count == 0:
        return False
    if not 0 <= verdict.restates < status.told_count:
        return False
    told_direction = status.told_directions[verdict.restates]
    flipped = (
        verdict.direction in _DIRECTIONAL and told_direction in _DIRECTIONAL and told_direction != verdict.direction
    )
    return not flipped


def decide(
    verdict: TriageVerdict,
    facts: GateFacts,
    status: StorylineStatus | None,
    *,
    hourly_cap_reached: bool = False,
    muted: bool = False,
    policy: DecidePolicy = DEFAULT_POLICY,
) -> DecisionResult:
    """Deterministic policy over the model's intent. Every path names its rule; nothing drops silently."""

    baseline = rule_baseline(facts)
    primaries = {_base(a.symbol) for a in verdict.assets if a.role == "primary"}
    grounded = {_base(s) for s in facts.grounded_assets}
    watch_hits = tuple(sorted(s for s in (primaries & grounded) if s in facts.watchlist_symbols))

    if muted:
        return DecisionResult("drop", "muted", None, baseline, watch_hits)
    if verdict.event_type == "noise":
        return DecisionResult("drop", "noise", None, baseline, watch_hits)
    if policy.restatement_drop and grounded_restatement(verdict, status):
        return DecisionResult("drop", "restatement", None, baseline, watch_hits)

    final: Decision
    rule: str | None = None
    if verdict.magnitude >= policy.escalate_magnitude and (
        verdict.direction in _DIRECTIONAL or verdict.scope == "macro"
    ):
        final, rule = "escalate", "magnitude3"
    elif facts.priority == "high" and verdict.decision == "push":
        final, rule = "escalate", "high_priority_push"
    elif (
        verdict.decision in _MODEL_WANTS_PUSH
        and verdict.actionable
        and verdict.magnitude >= policy.min_push_magnitude
        and verdict.direction != "unclear"
    ):
        final, rule = "push", "model_push_actionable"
    elif (
        verdict.direction == "unclear"
        and verdict.magnitude >= policy.unclear_push_min_magnitude
        and verdict.event_type in policy.unclear_push_event_types
        and verdict.decision != "drop"
    ):
        final, rule = "push", "unclear_but_clear_event"
    elif verdict.direction == "unclear":
        final, rule = "drop", "unclear_direction"
    elif watch_hits and verdict.magnitude >= policy.min_watchlist_magnitude:
        final, rule = "push", "watchlist"
    else:
        final, rule = "drop", "below_threshold"

    if final in {"push", "escalate"} and status is not None and policy.storyline_throttle:
        throttled_by = _storyline_throttle(verdict, status, final, policy)
        if throttled_by is not None:
            hard = _novel_bypass(verdict, status, final, policy)
            if hard is None:
                return DecisionResult("throttled", rule, throttled_by, baseline, watch_hits)
            if hard:
                return DecisionResult("throttled", rule, hard, baseline, watch_hits)
            rule = "novel_bypass"

    if final == "push" and hourly_cap_reached and policy.hourly_cap_enabled:
        return DecisionResult("throttled", rule, "hourly_cap", baseline, watch_hits)
    return DecisionResult(final, rule, None, baseline, watch_hits)


def _novel_bypass(verdict: TriageVerdict, status: StorylineStatus, final: Decision, policy: DecidePolicy) -> str | None:
    """A novel event (new_fact / progression, m >= novel_min_magnitude) may pass the soft throttle up to a hard cap.
    Returns None when the event is not novel enough (the soft throttle stands), "" when it passes, or the hard-cap
    ``throttled_by`` key when the hard cap is reached."""

    if verdict.novelty not in {"new_fact", "progression"} or verdict.magnitude < policy.novel_min_magnitude:
        return None
    if status.key.startswith("asset:"):
        pushed = status.pushed_2h if final == "push" else status.pushed_4h
        return "" if pushed < policy.asset_hard_cap_2h else f"storyline:{status.key}:hard{policy.asset_hard_cap_2h}"
    return (
        "" if status.pushed_4h < policy.theme_hard_cap_4h else f"storyline:{status.key}:hard{policy.theme_hard_cap_4h}"
    )


def _storyline_throttle(
    verdict: TriageVerdict, status: StorylineStatus, final: Decision, policy: DecidePolicy
) -> str | None:
    """Asset storylines: window-max plus direction flip. Theme/family storylines: at most ``theme_cap_4h`` pushes
    per 4 h, so a flood (a war, a rate shock) still lets its important progressions through."""

    if status.key.startswith("asset:"):
        window_max = status.max_magnitude_2h if final == "push" else status.max_magnitude_4h
        pushed = status.pushed_2h if final == "push" else status.pushed_4h
        seen = status.directions_2h if final == "push" else status.directions_4h
        if pushed > 0 and verdict.magnitude <= window_max and not _direction_flip(verdict.direction, seen):
            return f"storyline:{status.key}"
        return None
    if (
        status.pushed_4h >= policy.theme_cap_4h
        and verdict.magnitude <= status.max_magnitude_4h
        and not _direction_flip(verdict.direction, status.directions_4h)
    ):
        return f"storyline:{status.key}:cap{policy.theme_cap_4h}"
    return None


def fallback_verdict(facts: GateFacts, *, error_code: str) -> tuple[TriageVerdict, DecisionResult]:
    """Fail-closed degraded verdict when the model is unavailable."""

    baseline = rule_baseline(facts)
    verdict = TriageVerdict(
        novelty="new_fact",
        event_type="noise" if baseline == "drop" else "macro",
        assets=[],
        direction="neutral",  # a rule verdict has no view on direction; "unclear" would veto its own push
        scope="macro",
        magnitude=0 if baseline == "drop" else 2,
        actionable=baseline == "push",
        confidence=0.0,
        decision=baseline,
        headline_zh="模型不可用（规则兜底）",
        why_zh="",
    )
    return verdict, DecisionResult(baseline, "fail_closed_fallback", None, baseline)


def storyline_status_from_row(
    row: Mapping[str, Any] | None, key: str, *, told: Sequence[Mapping[str, Any]] = ()
) -> StorylineStatus:
    """``told`` is the ledger the model saw (status-bar order); only its directions matter to decide()."""

    told_directions = tuple(str(t.get("dir") or "") for t in told)
    if not row:
        return StorylineStatus(key=key, told_directions=told_directions)
    return StorylineStatus(
        key=key,
        pushed_2h=int(row.get("pushed_2h") or 0),
        pushed_4h=int(row.get("pushed_4h") or 0),
        max_magnitude_2h=int(row.get("max_magnitude_2h") or 0),
        max_magnitude_4h=int(row.get("max_magnitude_4h") or 0),
        directions_2h=tuple(row.get("directions_2h") or ()),
        directions_4h=tuple(row.get("directions_4h") or ()),
        last_push_ago_ms=(int(row["last_push_ago_ms"]) if row.get("last_push_ago_ms") is not None else None),
        told_directions=told_directions,
    )


__all__ = [
    "DEFAULT_POLICY",
    "ESCALATE_WINDOW_MS",
    "PUSH_WINDOW_MS",
    "DecidePolicy",
    "DecisionResult",
    "GateFacts",
    "StorylineStatus",
    "decide",
    "fallback_verdict",
    "grounded_restatement",
    "rule_baseline",
    "storyline_status_from_row",
]
