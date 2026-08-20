"""decide(): deterministic post-rules over the Triage verdict (pure, golden-tested)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, fields
from typing import Any, Final

from .models import Decision, TriageVerdict
from .similarity import max_similarity

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
    restatement_drop: bool = True
    # Policy v5 (issue #81): the storyline throttle stops counting and starts reading. A card the soft throttle
    # stopped is released when its Chinese headline resembles nothing the reader received in the window; the
    # counts survive only as a flood ceiling. Replayed over 2185 stored verdicts this cuts facts the reader never
    # received by 63% *and* near-duplicate pairs by 46% — the two used to trade against each other because the
    # only release was `novel_bypass`, the model's own unverified claim that its event was new.
    similarity_max: float = 0.25
    distinct_hard_cap_4h: int = 18
    distinct_asset_cap_2h: int = 6
    # Policy v4 (issue #77): the Gate's `priority` is an AMQP transport hint (score >= 90, watchlist, listing
    # frames, rate/yield macro), not a reader-facing importance judgment — it decides queue order, not the ⚡
    # header. It used to promote every high-priority push to `escalate`, which made every exchange listing notice
    # as loud as a missile strike. `escalate` is now magnitude-driven only; the rule still exists so the same
    # Events keep pushing, it just no longer shouts.
    high_priority_escalates: bool = False

    def as_dict(self) -> dict[str, Any]:
        """Every tunable, by name. A stored decision has to carry the numbers that produced it: without this the
        trace said which rule fired but not against which thresholds, so a historical verdict could not be
        replayed or compared with a candidate (#81). Also the policy half of the release-gate evidence."""

        out: dict[str, Any] = {}
        for spec in fields(self):
            value = getattr(self, spec.name)
            out[spec.name] = list(value) if isinstance(value, tuple) else value
        return out


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
    # Every card the reader actually received in the throttle window, newest first — not the <= 12 entries the
    # status bar showed the model. The two differ by design: the model gets a readable ledger, ``decide()`` gets
    # the whole window, and the wider set measurably catches more repeats (#81).
    seen_headlines: tuple[str, ...] = ()
    seen_event_ids: tuple[str, ...] = ()

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
    # Only set when the storyline throttle fired and the card was measured against the reader's window:
    # how close it came, and which of ``status.seen_*`` it came closest to (-1 = nothing to compare against).
    seen_similarity: float | None = None
    seen_against: int = -1


def _base(symbol: str) -> str:
    return symbol.upper().replace("XYZ-", "")


_BASELINE_MIN_SCORE: Final = 80.0


def rule_baseline(facts: GateFacts, *, fail_open_high_priority: bool = True) -> Decision:
    """The decision a pure-rule system would take with no model at all.

    Watchlist, or a provider score >= 80 on a grounded asset, pushes. Since #81 a high-priority Event and a
    deterministic exchange listing notice push too: the model being unavailable is not evidence that a missile
    strike or a delisting does not matter, and a degraded card renders the wire headline, which is a usable card.
    Before this, a model outage silently dropped every high-priority Event without a grounded asset — and the
    watchlist half of the old rule is inert on a deployment whose `news.watchlist` is empty, which is the live
    one. Everything else drops, counted as degraded, never silently.
    """

    watch = any(_base(s) in facts.watchlist_symbols for s in facts.grounded_assets)
    score = float(facts.provider_score or 0)
    if watch or (score >= _BASELINE_MIN_SCORE and facts.grounded_assets):
        return "push"
    if fail_open_high_priority and (facts.priority == "high" or facts.admission == "listing_deterministic"):
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
    degraded: bool = False,
    policy: DecidePolicy = DEFAULT_POLICY,
) -> DecisionResult:
    """Deterministic policy over the model's intent. Every path names its rule; nothing drops silently.

    ``degraded`` marks a rule-baseline fallback verdict (no model judgment): its headline is a placeholder, so it
    is never released from the storyline throttle — a placeholder is not evidence that a fact is new.

    Policy v5 (#81) removed the last path where the model's own unverified claim about itself opened a gate:
    ``novelty`` is still read, but only in the direction that *withholds* a card (a restatement of a ledger entry
    the model was actually shown, direction unflipped), never to promote one. Whether a card gets past the
    storyline throttle is now decided by measuring it against what the reader received.
    """

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
        # Recall-preserving on purpose: this branch pushes without requiring `actionable` or min_push_magnitude,
        # so it must stay a branch. Only its loudness changes (#77).
        final = "escalate" if policy.high_priority_escalates else "push"
        rule = "high_priority_push"
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

    seen_similarity: float | None = None
    seen_against = -1
    if final in {"push", "escalate"} and status is not None and policy.storyline_throttle:
        throttled_by = _storyline_throttle(verdict, status, final, policy)
        if throttled_by is not None:
            if degraded or policy.similarity_max <= 0.0:
                # A rule-baseline card carries the wire headline as a placeholder, not a judged statement of what
                # is new; and `similarity_max = 0` is the operator switching the content judgment off, which
                # leaves the pre-v5 count cap. Either way: no release.
                return DecisionResult("throttled", rule, throttled_by, baseline, watch_hits)
            seen_similarity, seen_against = max_similarity(verdict.headline_zh, status.seen_headlines)
            # Nothing comparable in the window (an empty ledger, or a headline too short to bigram) is not
            # evidence of a repeat: the reader received nothing, so nothing can be a repeat of it.
            if seen_against >= 0 and seen_similarity >= policy.similarity_max:
                # The reader has this. This is the whole duplicate defence: everything else is a ceiling.
                return DecisionResult(
                    "throttled", rule, f"{throttled_by}:seen", baseline, watch_hits, seen_similarity, seen_against
                )
            ceiling = _flood_ceiling(status, policy)
            if ceiling is not None:
                return DecisionResult("throttled", rule, ceiling, baseline, watch_hits, seen_similarity, seen_against)
            rule = "distinct_bypass"

    if final == "push" and hourly_cap_reached and policy.hourly_cap_enabled:
        return DecisionResult("throttled", rule, "hourly_cap", baseline, watch_hits, seen_similarity, seen_against)
    return DecisionResult(final, rule, None, baseline, watch_hits, seen_similarity, seen_against)


def _flood_ceiling(status: StorylineStatus, policy: DecidePolicy) -> str | None:
    """The absolute number of cards one storyline may spend in its window, whatever they say.

    This is not a relevance judgment — a distinct card is only stopped here because the reader cannot read an
    unbounded stream about one topic. It is deliberately far above the soft cap (18 per theme per 4 h, 6 per
    asset per 2 h) so that it fires on a genuine flood and not on an ordinary busy hour.
    """

    if status.key.startswith("asset:"):
        if status.pushed_2h >= policy.distinct_asset_cap_2h:
            return f"storyline:{status.key}:hard{policy.distinct_asset_cap_2h}"
        return None
    if status.pushed_4h >= policy.distinct_hard_cap_4h:
        return f"storyline:{status.key}:hard{policy.distinct_hard_cap_4h}"
    return None


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


def fallback_verdict(facts: GateFacts, *, error_code: str, title: str = "") -> tuple[TriageVerdict, DecisionResult]:
    """Fail-closed degraded verdict when the model is unavailable. ``headline_zh`` carries the wire headline (the
    console and the context line show what the Event is, not that the model failed; the card renders the wire text
    itself, see delivery)."""

    baseline = rule_baseline(facts)
    wire_headline = " ".join(str(title or "").split())[:60] or "模型不可用（规则兜底）"
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
        headline_zh=wire_headline,
        why_zh="",
    )
    return verdict, DecisionResult(baseline, "fail_closed_fallback", None, baseline)


def storyline_status_from_row(
    row: Mapping[str, Any] | None,
    key: str,
    *,
    told: Sequence[Mapping[str, Any]] = (),
    seen: Sequence[Mapping[str, Any]] | None = None,
) -> StorylineStatus:
    """``told`` is the ledger the model saw (status-bar order); only its directions matter to decide().

    ``seen`` is every card the reader received in the window — the wider set decide() measures a throttled card
    against. It defaults to ``told`` so pure callers and replays that only kept the status bar still work, at the
    cost of a narrower comparison than the worker performs.
    """

    told_directions = tuple(str(t.get("dir") or "") for t in told)
    rows = list(told if seen is None else seen)
    seen_headlines = tuple(str(r.get("headline_zh") or "") for r in rows)
    seen_event_ids = tuple(str(r.get("event_id") or "") for r in rows)
    if not row:
        return StorylineStatus(
            key=key,
            told_directions=told_directions,
            seen_headlines=seen_headlines,
            seen_event_ids=seen_event_ids,
        )
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
        seen_headlines=seen_headlines,
        seen_event_ids=seen_event_ids,
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
