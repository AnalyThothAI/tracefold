"""decide(): deterministic post-rules over the Triage verdict (pure, golden-tested)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, fields
from typing import Any, Final

from .models import Decision, TriageVerdict
from .similarity import max_similarity

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
    restatement_drop: bool = True
    # Duplicate protection is content-based, never a reader quota. A value of
    # zero disables the deterministic similarity check; it does not restore a
    # count cap. Escalations remain exempt because a false-positive similarity
    # match is least affordable for the most important cards.
    similarity_max: float = 0.25
    # Policy v4 (issue #77): the Gate's `priority` is an AMQP transport hint (score >= 90, watchlist, listing
    # frames, rate/yield macro), not a reader-facing importance judgment — it decides queue order, not the ⚡
    # header. It used to promote every high-priority push to `escalate`, which made every exchange listing notice
    # as loud as a missile strike. `escalate` is now magnitude-driven only; the rule still exists so the same
    # Events keep pushing, it just no longer shouts.
    high_priority_escalates: bool = False
    # Policy v8 (recall-first). A `noise` label used to return before every other rule, so one
    # mislabelled enum outranked magnitude 3, Gate priority and the watchlist. The ceiling is 1, not
    # 0, because the Program instruction genuinely allows noise at magnitude 1: it calls magnitude 1
    # "a routine update on one name that changes nothing" and its own worked example files a user
    # milestone as magnitude 1 / drop. Magnitude 2 is where the instruction says "clearly tradable",
    # so a verdict that says noise and 2 in the same breath is disagreeing with itself. Measured over
    # 300 replayed Events (2026-08-22): 14 of v4's noise drops carried magnitude >= 2 — among them a
    # hijacked tanker in the Gulf of Aden the Gate had flagged high priority and the previous
    # generation had delivered. Raise this to let `noise` veto louder verdicts again; it is the one
    # knob that trades recall for quiet.
    noise_veto_max_magnitude: int = 1
    # A high-priority Event is never dropped by the `noise` label alone: priority is upstream Gate
    # evidence the model did not produce.
    noise_veto_respects_gate_priority: bool = True
    # The Gate flagged the Event high priority and the model itself assigned this magnitude or more
    # while still asking to hold. Recall-first, that disagreement resolves toward the reader. Zero
    # disables the rule, and it never fires on a normal-priority Event.
    contested_push_min_magnitude: int = 2
    # Exchange listing/delisting frames are independent facts wearing one template: "Coinbase adds
    # ALIGN" and "Upbit adds BICO" name different instruments but share almost every character
    # bigram, so both the model's restatement judgment and the deterministic similarity check read
    # the second one as a repeat. #72 admits these frames deterministically; this stops that
    # admission from being undone one step later. The trade is explicit: a genuine re-send of the
    # same notice is no longer withheld by content.
    listing_exempt_from_duplicate: bool = True

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
    """Content evidence from cards proven to have reached the reader.

    The small ``told`` subset grounds the model's restatement citation.  The
    wider ``seen`` subset supports deterministic same-fact comparison.  It
    intentionally carries no delivery counts or capacity state.
    """

    key: str
    told_directions: tuple[str, ...] = ()
    # Same order as ``told_directions``: the instruments each shown ledger entry was about, so a
    # restatement claim can be checked against the asset it cites rather than its rendered prose.
    told_assets: tuple[frozenset[str], ...] = ()
    # Every card the reader actually received in the comparison window, newest first — not the <= 12 entries the
    # status bar showed the model. The two differ by design: the model gets a readable ledger, ``decide()`` gets
    # the whole window, and the wider set measurably catches more repeats (#81).
    seen_headlines: tuple[str, ...] = ()
    seen_event_ids: tuple[str, ...] = ()
    # The direction of each ``seen_headlines`` entry, same order. A reversal of a fact the reader just received
    # shares almost every character bigram with it ("SEC 批准…" vs "SEC 拒绝…" scores 0.60), so the duplicate
    # defence has to be able to tell the two apart.
    seen_directions: tuple[str, ...] = ()
    # The instruments each remembered card was about, same order. `headline_zh` is Chinese reader prose and the
    # reader contract strips parenthesised tickers, so the rendered text cannot answer "is this the same asset?";
    # only a structured field can. Empty for a caller that did not supply assets, which never grants an exemption.
    seen_assets: tuple[frozenset[str], ...] = ()

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
    # Only set when the card was measured against the reader's window: how close it came, and which of
    # ``status.seen_*`` it came closest to (-1 = nothing to compare against).
    seen_similarity: float | None = None
    seen_against: int = -1
    # ``all`` means the ordinary push path was compared with the sent ledger;
    # empty means no comparison was made.
    seen_scope: str = ""


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


def _noise_veto_applies(verdict: TriageVerdict, facts: GateFacts, policy: DecidePolicy) -> bool:
    """True when a ``noise`` label may drop the card on the strength of that label alone.

    ``noise`` is defined by the Program's own instruction as magnitude 0 material. Any verdict that
    labels an Event noise and then gives it weight — magnitude above the veto ceiling, ``actionable``,
    or a push intent — is disagreeing with itself, and a self-contradicting enum must not outrank the
    rules below it. The Gate's high priority is treated the same way: it is upstream evidence the
    model did not produce, so it survives a noise label unless the operator turns that off.
    """

    if verdict.magnitude > policy.noise_veto_max_magnitude:
        return False
    if verdict.actionable or verdict.decision in _MODEL_WANTS_PUSH:
        return False
    # Gate priority is upstream evidence the model did not produce, so it survives a noise label.
    return not (policy.noise_veto_respects_gate_priority and facts.priority == "high")


# Exchange listing notices: one wire template carrying different instruments. "Coinbase adds ALIGN"
# and "Upbit adds BICO" share almost every character bigram while naming different tradable things,
# so the duplicate check needs the instrument, not the prose (#72).
_TEMPLATE_ADMISSIONS: Final = frozenset({"listing_deterministic"})

# Open-interest telemetry: repeats are already bounded, by the rank ceiling inside the lane's own
# evaluator (#137). Two frames for one symbol are two different observations — different change,
# different value, different rank — and the reader asked for the opening ones by count. Running the
# content check over them as well would silently halve that count: `WINDOW_MS` and `TOLD_WINDOW_MS`
# are both 4 h, so a rank-2 frame is always inside its rank-1 sibling's ledger, and the two headlines
# score 0.41 against a 0.25 threshold. The measured "first two per symbol" would have shipped as
# "one per symbol". A byte-identical repeat is still collapsed upstream by the exact fingerprint.
_RANK_BOUNDED_ADMISSIONS: Final = frozenset({"telemetry_deterministic"})


def _template_fact(facts: GateFacts) -> bool:
    """True for a Gate-admitted frame whose text is a template carrying an instrument.

    Only the Gate's admission counts. ``verdict.event_type`` is unverified model output, so trusting
    it would let any story the model repeatedly types as ``listing`` — a recurring "X will support Y"
    tease, an ETF-approval rumour thread — escape duplicate evidence on every repeat with no
    corroboration. The admission is derived upstream from provider metadata.
    """

    return facts.admission in _TEMPLATE_ADMISSIONS


def _names_another_instrument(
    template_fact: bool,
    symbols: set[str],
    seen_assets: Sequence[frozenset[str]],
    index: int,
) -> bool:
    """True when a template frame's closest match is a card about a *different* instrument.

    Exchange notices share one wire template, so "Coinbase 将新增对 ALIGN 的支持" and "Upbit 将新增对
    BICO 的交易支持" score well above ``similarity_max`` on character bigrams while naming different
    tradable things. Exempting the whole class would stop protecting the reader from a genuinely
    re-issued notice, so the exemption is narrowed to the case it exists for.

    The comparison is between symbol sets, never between a ticker and rendered headline text: the
    reader contract tells the model to strip parenthesised tickers and write Chinese, so a substring
    test would both miss the common case and fire by accident (``BASE`` inside "Coinbase"). A ledger
    row that carries no assets is not evidence of a different instrument and never exempts.
    """

    if not template_fact or not symbols or not 0 <= index < len(seen_assets):
        return False
    matched = seen_assets[index]
    return bool(matched) and matched.isdisjoint(symbols)


def _seen_flip(direction: str, seen_directions: Sequence[str], index: int) -> bool:
    """True when the card resembles a ledger entry it *contradicts* — the reader was told bullish and this is
    bearish, or the other way round. Only a directional pair counts: neutral/unclear on either side is not a
    reversal, and a ledger without directions (a pure caller, an old replay) never exempts anything."""

    if direction not in _DIRECTIONAL or not 0 <= index < len(seen_directions):
        return False
    told = seen_directions[index]
    return told in _DIRECTIONAL and told != direction


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
    degraded: bool = False,
    policy: DecidePolicy = DEFAULT_POLICY,
) -> DecisionResult:
    """Deterministic policy over the model's intent. Every path names its rule; nothing drops silently.

    Runtime policy has no hourly, 2-hour, or 4-hour reader quota, and no
    operator mute: once the semantic conditions resolve to push/escalate, only
    duplicate evidence may withhold the card. ``degraded`` fallback cards skip
    similarity because their wire headline is not a semantic judgment.
    """

    baseline = rule_baseline(facts)
    primaries = {_base(a.symbol) for a in verdict.assets if a.role == "primary"}
    grounded = {_base(s) for s in facts.grounded_assets}
    watch_hits = tuple(sorted(s for s in (primaries & grounded) if s in facts.watchlist_symbols))

    if verdict.event_type == "noise" and _noise_veto_applies(verdict, facts, policy):
        return DecisionResult("drop", "noise", None, baseline, watch_hits)
    template_fact = policy.listing_exempt_from_duplicate and _template_fact(facts)
    # The restatement bypass is narrowed the same way the similarity one is. Exempting the class
    # outright left a listing frame with no duplicate defence at all whenever the similarity check
    # never ran — an `escalate`, or a deployment with `similarity_max: 0`.
    template_restates_other = template_fact and _names_another_instrument(
        template_fact, primaries | grounded, status.told_assets if status else (), verdict.restates
    )
    if policy.restatement_drop and not template_restates_other and grounded_restatement(verdict, status):
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
        facts.priority == "high"
        and policy.contested_push_min_magnitude > 0
        and verdict.magnitude >= policy.contested_push_min_magnitude
        and verdict.decision not in _MODEL_WANTS_PUSH
        and (verdict.direction in _DIRECTIONAL or verdict.scope == "macro")
    ):
        # The Gate called this Event high priority and the model itself weighed it at magnitude 2 or
        # more, then asked to hold it anyway. That is a disagreement between upstream evidence and a
        # single model field, not a considered hold, and recall-first it resolves toward the reader.
        # Two guards keep this from being a Gate-priority push. `priority` is
        # `score >= 90 or watchlist or listing or macro lexicon`, which #77 calls an AMQP transport
        # hint, so without them a provider-scored price-only frame the model rejected on every field
        # — the instruction's own "Spot Palladium Rises Nearly 3%" negative example — would reach
        # the reader. The rule is about a *hold* intent, so a verdict that asked to push or escalate
        # belongs to `high_priority_push`/`model_push_actionable` and keeps their attribution.
        # Requiring a direction or macro scope (rather than `actionable`) also keeps this branch from
        # sitting above `unclear_direction` and quietly bypassing `unclear_push_event_types`.
        # The rule names itself in the trace so the operator sees which of the two won.
        final, rule = "push", "contested_high_priority"
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
    seen_scope = ""
    rank_bounded = facts.admission in _RANK_BOUNDED_ADMISSIONS
    if final == "push" and status is not None and not degraded and not rank_bounded and policy.similarity_max > 0.0:
        seen_scope = "all"
        seen_similarity, seen_against = max_similarity(verdict.headline_zh, status.seen_headlines)
        if (
            seen_against >= 0
            and seen_similarity >= policy.similarity_max
            and not _seen_flip(verdict.direction, status.seen_directions, seen_against)
            and not _names_another_instrument(template_fact, primaries | grounded, status.seen_assets, seen_against)
        ):
            return DecisionResult(
                "throttled",
                rule,
                f"storyline:{status.key}:seen",
                baseline,
                watch_hits,
                seen_similarity,
                seen_against,
                seen_scope,
            )
    return DecisionResult(final, rule, None, baseline, watch_hits, seen_similarity, seen_against, seen_scope)


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


def _row_symbols(row: Mapping[str, Any]) -> frozenset[str]:
    """Every symbol a ledger row was about, from the Gate's grounded tags and the verdict's assets."""

    symbols = {_base(str(value)) for value in row.get("grounded_assets") or () if value}
    for asset in row.get("assets") or ():
        symbol = asset.get("symbol") if isinstance(asset, Mapping) else asset
        if symbol:
            symbols.add(_base(str(symbol)))
    return frozenset(symbol for symbol in symbols if symbol)


def storyline_status(
    key: str,
    *,
    told: Sequence[Mapping[str, Any]] = (),
    seen: Sequence[Mapping[str, Any]] | None = None,
) -> StorylineStatus:
    """``told`` is the ledger the model saw (status-bar order); only its directions matter to decide().

    ``seen`` is every card the reader received in the window — the wider set decide() measures a duplicate candidate
    against. It defaults to ``told`` so pure callers and replays that only kept the status bar still work, at the
    cost of a narrower comparison than the worker performs.
    """

    told_directions = tuple(str(t.get("dir") or "") for t in told)
    told_assets = tuple(_row_symbols(t) for t in told)
    rows = list(told if seen is None else seen)
    seen_headlines = tuple(str(r.get("headline_zh") or "") for r in rows)
    seen_event_ids = tuple(str(r.get("event_id") or "") for r in rows)
    # `told` rows spell the direction `dir`, ledger rows spell it `direction`; a row that carries neither leaves
    # an empty string, which `_seen_flip` reads as "no opinion" and never exempts on.
    seen_directions = tuple(str(r.get("direction") or r.get("dir") or "") for r in rows)
    seen_assets = tuple(_row_symbols(r) for r in rows)
    return StorylineStatus(
        key=key,
        told_directions=told_directions,
        told_assets=told_assets,
        seen_headlines=seen_headlines,
        seen_event_ids=seen_event_ids,
        seen_directions=seen_directions,
        seen_assets=seen_assets,
    )


__all__ = [
    "DEFAULT_POLICY",
    "DecidePolicy",
    "DecisionResult",
    "GateFacts",
    "StorylineStatus",
    "decide",
    "fallback_verdict",
    "grounded_restatement",
    "rule_baseline",
    "storyline_status",
]
