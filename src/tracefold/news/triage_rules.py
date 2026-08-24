"""decide(): deterministic post-rules over the Triage verdict (pure, golden-tested)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, fields
from typing import Any, Final

from .models import Decision, TriageVerdict, base_symbol
from .program.contracts import EditorialEnvelope, ScoredJudgment, TradeRelevanceV1
from .similarity import max_similarity

# One owner for the withhold key: `outcome` renders it, `repository` counts it.
STALE_SOURCE_KEY: Final = "artifact:stale"

_DIRECTIONAL = frozenset({"bullish", "bearish"})


@dataclass(frozen=True, slots=True)
class DecidePolicy:
    """The four v10 safety/duplicate knobs exposed through ``news.policy``.

    Trade relevance and objective guards are a code-owned ordered policy, not
    operator-tunable thresholds.
    """

    restatement_drop: bool = True
    # Duplicate protection is content-based, never a reader quota. A value of
    # zero disables the deterministic similarity check; it does not restore a
    # count cap. Escalations remain exempt because a false-positive similarity
    # match is least affordable for the most important cards.
    similarity_max: float = 0.25
    # An artifact older than this when the provider pushed it is a replay, not news (#154). The artifact ledger
    # catches a re-send of something we already delivered; this catches the case it cannot see — a stale artifact
    # arriving for the first time, such as the 16-day-old tweet that shipped as "Take-Two 股票 $TTWO 周四在
    # Solana 上线". Measured over 3174 x/twitter frames in 30 days the distribution is bimodal: 2491 within 10 s
    # of the push, 7 beyond 16 h, nothing between, and never negative — so any threshold in [10 min, 16 h] picks
    # exactly the same frames. 12 h is the `general` family window: older than that and our own dedup could no
    # longer have seen the first delivery. Zero disables the rule.
    stale_source_max_age_s: int = 12 * 60 * 60
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
    admission: str
    # Seconds between the source artifact's own publication and the provider's push (#154). `None` whenever the
    # artifact does not carry its own timestamp, which is every non-x/twitter frame.
    source_age_s: int | None = None


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


_base = base_symbol


def grounded_watchlist_hits(facts: GateFacts) -> tuple[str, ...]:
    """Objective watchlist facts, independent of any model-selected primary."""

    return tuple(sorted({_base(s) for s in facts.grounded_assets} & facts.watchlist_symbols))


def rule_baseline(facts: GateFacts) -> Decision:
    """The v10 degraded baseline: only objective guards fail open."""

    if facts.admission in {"listing_deterministic", "telemetry_deterministic"}:
        return "push"
    return "push" if grounded_watchlist_hits(facts) else "drop"


def realtime_eligible(verdict: TriageVerdict, relevance: TradeRelevanceV1) -> bool:
    """The code-owned trade-attention eligibility predicate from policy v10."""

    direct_surface = (
        relevance.tradability in {"direct", "second_order"}
        and bool(relevance.channels)
        and bool(relevance.affected_markets)
    )
    material_change = relevance.development_delta == "state_change" or (
        relevance.development_delta == "material_detail"
        and (relevance.tradability == "direct" or relevance.surprise in {"unscheduled", "material_vs_expectation"})
    )
    return verdict.magnitude >= 2 and direct_surface and material_change


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
    judgment: ScoredJudgment,
    facts: GateFacts,
    status: StorylineStatus | None,
    *,
    degraded: bool = False,
    policy: DecidePolicy = DEFAULT_POLICY,
) -> DecisionResult:
    """Deterministic policy over one atomically identified editorial judgment.

    Runtime policy has no hourly, 2-hour, or 4-hour reader quota, and no
    operator mute: once the semantic conditions resolve to push/escalate, only
    duplicate evidence may withhold the card. ``degraded`` fallback cards skip
    similarity because their wire headline is not a semantic judgment.
    """

    verdict = judgment.verdict
    editorial = judgment.editorial
    baseline = rule_baseline(facts)
    primaries = {_base(a.symbol) for a in verdict.assets if a.role == "primary"}
    grounded = {_base(s) for s in facts.grounded_assets}
    watch_hits = grounded_watchlist_hits(facts)

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
    rule: str | None
    if degraded or editorial.editorial_origin == "degraded_unavailable":
        if facts.admission == "listing_deterministic":
            final, rule = "push", "degraded_listing_objective"
        elif facts.admission == "telemetry_deterministic":
            final, rule = "push", "degraded_telemetry_objective"
        elif watch_hits:
            final, rule = "push", "degraded_watchlist_objective"
        else:
            final, rule = "drop", "degraded_no_objective_guard"
    elif facts.admission == "listing_deterministic":
        final, rule = "push", "listing_deterministic"
    elif facts.admission == "telemetry_deterministic":
        # This intent is arithmetic output from the code-owned OI lane, never a
        # model delivery opinion. It preserves the bounded rank rule.
        final = verdict.decision
        rule = "telemetry_deterministic"
    elif watch_hits:
        final, rule = "push", "watchlist_objective_guard"
    elif editorial.editorial_origin != "model" or editorial.relevance is None:
        final, rule = "drop", "trade_relevance_inconsistent"
    elif editorial.relevance.reader_value == "escalate" and realtime_eligible(verdict, editorial.relevance):
        final, rule = "escalate", "trade_relevance_escalate"
    elif editorial.relevance.reader_value == "realtime" and realtime_eligible(verdict, editorial.relevance):
        final, rule = "push", "trade_relevance_realtime"
    elif editorial.relevance.reader_value in {"background", "none"}:
        final, rule = "drop", f"reader_value_{editorial.relevance.reader_value}"
    else:
        final, rule = "drop", "trade_relevance_inconsistent"

    # #154: a replay is not a push, whatever the verdict says about it. `escalate` is exempt for the same reason
    # it is exempt from the similarity check — a false positive is least affordable on the loudest cards — and a
    # degraded verdict is exempt because the wire-headline fallback is already the conservative path.
    if (
        final == "push"
        and not degraded
        and policy.stale_source_max_age_s > 0
        and facts.source_age_s is not None
        and facts.source_age_s > policy.stale_source_max_age_s
    ):
        # A constant key on purpose: `throttled_by` is folded into a top-10 count map, so embedding the age
        # would give every withhold its own count-1 bucket and hide the rule from `status.pipeline`. The age
        # itself is in the trace.
        return DecisionResult("throttled", "stale_source_artifact", STALE_SOURCE_KEY, baseline, watch_hits)

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


def fallback_verdict(facts: GateFacts, *, error_code: str, title: str = "") -> tuple[ScoredJudgment, DecisionResult]:
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
    judgment = ScoredJudgment.issue(
        verdict=verdict,
        editorial=EditorialEnvelope.issue(editorial_origin="degraded_unavailable", relevance=None),
    )
    return judgment, decide(judgment, facts, None, degraded=True)


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
    "grounded_watchlist_hits",
    "realtime_eligible",
    "rule_baseline",
    "storyline_status",
]
