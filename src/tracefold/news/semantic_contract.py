"""Framework-neutral public Interface for News semantic judgment.

Callers construct one immutable :class:`TriageContext` and invoke one
``SemanticJudge.judge`` method.  DSPy, Predictor state, Program artifacts,
model routes and compiler state are deliberately absent from this module.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final, Literal, Protocol, cast, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .artifact_identity import canonical_sha
from .models import TriageVerdict, base_symbol
from .similarity import similarity

TOLD_WINDOW_MS: Final[int] = 4 * 3_600_000
# The bounded sent ledger the selector reads, and the bounded slice the model sees.  The gap between them is
# the point: `decide()` keeps governing duplicates against the whole window while the Program reads a compact,
# candidate-conditioned view of it.
TOLD_SOURCE_MAX: Final[int] = 128
# 16, not 12. Measured on every accepted restatement whose duplicate target was inside the 4 h ledger (n=22),
# target recall@N: 12 rows recalls 19, 16 rows recalls 21. The binding constraint on the old selector was never
# the ranking — a dense storyline puts 18-22 genuinely related cards in one window — so no ordering recovers
# what the cap excludes. ReaderCard no longer receives the ledger at all, which more than pays for the four
# extra rows: the two-call total moves ~+2%.
TOLD_MAX: Final[int] = 16
# No tier may take every slot. Ranking storyline first and filling the rest by tier order scored *below* the
# predecessor (18/22 against 19/22): with 14-17 same-storyline cards in the window, tier 1 consumed all 16 rows
# and the shared-instrument evidence that actually held the duplicate never got one. Capping the storyline tier
# is what makes the lower tiers reachable.
TOLD_STORYLINE_TIER_MAX: Final[int] = 8
TOLD_SYMBOLS_MAX: Final[int] = 6
# Retrieval's own threshold on comparison titles, deliberately not `news.policy.similarity_max`: that knob is
# operator-owned duplicate policy over reader headlines, and coupling the two would let a policy edit silently
# change what the model is allowed to see.
TOLD_FACT_SIMILARITY_MIN: Final[float] = 0.25
WATCHLIST_MAX: Final[int] = 64
GROUNDED_ASSETS_MAX: Final[int] = 16
STRATEGIES_MAX: Final[int] = 16

ToldTier = Literal["storyline", "asset_overlap", "fact_similarity", "recency"]
# Deterministic ordered union.  There is no learned score and no undocumented numeric weight.
#
# "same family" is deliberately absent: 96% of sent cards carry `family='general'`, so that tier would have
# been a relabelled recency tier.  "same storyline theme" is absent for the opposite reason — two cards with
# the same `theme:` key are already an exact storyline match, so the tier could never fire.
TOLD_TIER_ORDER: Final[tuple[ToldTier, ...]] = ("storyline", "asset_overlap", "fact_similarity", "recency")
TOLD_SELECTOR_ID: Final[str] = "told_context_selector_v1"
# Stable bundle identity for retrieval.  Anything that changes what the model is shown — the ledger truth and
# its projection, the window, the source cap, the tier order, the comparison primitives, the 12-entry cap, or
# the model-visible schema — has to change this hash, or a selector edit would ship as the same arm.
TOLD_SELECTOR_SHA256: Final[str] = canonical_sha(
    {
        "selector": TOLD_SELECTOR_ID,
        "source_truth": "news_deliveries(kind=first,state=sent)",
        "source_projection": [
            "event_id",
            "at_ms",
            "storyline_key",
            "event_type",
            "magnitude",
            "direction",
            "headline_zh",
            "grounded_assets",
            "assets",
            "comparison_title",
        ],
        "window_ms": TOLD_WINDOW_MS,
        "source_max": TOLD_SOURCE_MAX,
        "tier_order": list(TOLD_TIER_ORDER),
        "symbol_primitive": "base_symbol_v1",
        "similarity_primitive": "character_bigram_jaccard_v1",
        "similarity_field": "comparison_title",
        "similarity_min": TOLD_FACT_SIMILARITY_MIN,
        "rank_order": ["tier", "-similarity", "-at_ms", "event_id"],
        "storyline_tier_max": TOLD_STORYLINE_TIER_MAX,
        "dedup": "event_id",
        "excludes_candidate": True,
        "visible_cap": TOLD_MAX,
        "visible_fields": ["i", "ago_min", "key", "type", "sym", "m", "dir", "headline_zh"],
    }
)


class _ExactContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FrozenEventEvidence(_ExactContractModel):
    """Immutable evidence identity plus the bounded evidence visible to the Program."""

    event_id: str
    evidence_version: int = Field(ge=0)
    evidence_sha256: str
    focus_fact_id: str
    source: str = ""
    strategies: tuple[str, ...] = Field(default=(), max_length=STRATEGIES_MAX)
    engine_type: str = "unknown"
    title: str = Field(max_length=600)
    raw_first_line: str = Field(default="", max_length=300)
    content: str = Field(default="", max_length=600)
    published_at_ms: int = Field(ge=0)
    member_count: int = Field(default=1, ge=1)
    family: str = "general"
    provider_score: int | None = None
    provider_coins: tuple[str, ...] = Field(default=(), max_length=10)
    priority: str = "normal"
    # Selector input only, never rendered: the Deduper's normalized title, which is what a same-fact
    # comparison against a prior Event is actually made of.  The model reads `title`.
    comparison_title: str = Field(default="", max_length=600)


class SemanticGateContext(_ExactContractModel):
    asset_class: str = "none"
    grounded_assets: tuple[str, ...] = Field(default=(), max_length=GROUNDED_ASSETS_MAX)
    macro_lexicon: bool = False
    pr_template: bool = False


class ToldLedgerEntry(_ExactContractModel):
    """One card the reader already received, as the selector chose it.

    ``event_id``, ``at_ms``, ``tier`` and ``similarity`` are audit-only: they answer "why was this row shown"
    without ever reaching a Predictor.
    """

    i: int = Field(ge=0)
    event_id: str
    at_ms: int = Field(ge=0)
    ago_min: int = Field(ge=0)
    storyline_key: str = ""
    event_type: str = ""
    symbols: tuple[str, ...] = Field(default=(), max_length=TOLD_SYMBOLS_MAX)
    magnitude: int = Field(ge=0, le=3)
    direction: str
    headline_zh: str = Field(max_length=60)
    tier: ToldTier = "recency"
    similarity: float = Field(default=0.0, ge=0.0, le=1.0)


def _row_symbols(row: Mapping[str, Any]) -> frozenset[str]:
    """Every instrument one ledger row was about: the Gate's grounded tags plus the verdict's own assets."""

    symbols = {base_symbol(str(value)) for value in row.get("grounded_assets") or () if value}
    for asset in row.get("assets") or ():
        symbol = asset.get("symbol") if isinstance(asset, Mapping) else asset
        if symbol:
            symbols.add(base_symbol(str(symbol)))
    return frozenset(symbol for symbol in symbols if symbol)


def _take_with_tier_caps(
    ranked: Sequence[tuple[int, float, int, str, Mapping[str, Any], ToldTier, float]],
    *,
    limit: int,
) -> list[tuple[int, float, int, str, Mapping[str, Any], ToldTier, float]]:
    """Fill the slots in rank order, but never let the storyline tier take them all.

    A capped tier's overflow is not discarded: once every other tier has had its chance, the leftovers fill any
    remaining slots in the same rank order, so the result is still a total order over the same rows.
    """

    caps = {TOLD_TIER_ORDER.index("storyline"): TOLD_STORYLINE_TIER_MAX}
    chosen: list[tuple[int, float, int, str, Mapping[str, Any], ToldTier, float]] = []
    overflow: list[tuple[int, float, int, str, Mapping[str, Any], ToldTier, float]] = []
    used: dict[int, int] = {}
    for item in ranked:
        if len(chosen) >= limit:
            break
        tier_index = item[0]
        if used.get(tier_index, 0) >= caps.get(tier_index, limit):
            overflow.append(item)
            continue
        used[tier_index] = used.get(tier_index, 0) + 1
        chosen.append(item)
    for item in overflow:
        if len(chosen) >= limit:
            break
        chosen.append(item)
    return chosen[:limit]


class ToldLedgerSnapshot(_ExactContractModel):
    """The bounded, candidate-conditioned slice of the sent ledger that ``EventSemantics`` may read."""

    storyline_key: str
    preliminary: bool = True
    entries: tuple[ToldLedgerEntry, ...] = Field(default=(), max_length=TOLD_MAX)
    source_count: int = Field(default=0, ge=0)

    @classmethod
    def select(
        cls,
        rows: Sequence[Mapping[str, Any]],
        *,
        now_ms: int,
        storyline_key: str,
        symbols: Sequence[str] = (),
        comparison_title: str = "",
        exclude_event_id: str = "",
        limit: int = TOLD_MAX,
    ) -> ToldLedgerSnapshot:
        """Rank the bounded sent ledger against *this* candidate and keep the top ``limit`` rows.

        The tiers are an ordered union, not a quota: nothing is reserved for recency, and a candidate whose
        storyline already fills every slot simply gets no unrelated cards.  Inside a tier, positive same-fact
        similarity ranks first, then sent time newest-first, then the stable Event identity — so the same
        ledger always produces the same selection, whatever order the database returned it in.
        """

        bounded = max(0, min(int(limit), TOLD_MAX))
        cutoff = int(now_ms) - TOLD_WINDOW_MS
        candidate_symbols = frozenset(base_symbol(str(value)) for value in symbols if value)
        candidate_title = str(comparison_title or "")

        window = sorted(
            (row for row in rows if int(row.get("at_ms") or 0) >= cutoff),
            key=lambda row: -int(row.get("at_ms") or 0),
        )[:TOLD_SOURCE_MAX]

        ranked: list[tuple[int, float, int, str, Mapping[str, Any], ToldTier, float]] = []
        deduped: set[str] = set()
        for row in window:
            event_id = str(row.get("event_id") or "")
            if not event_id or event_id == exclude_event_id or event_id in deduped:
                continue
            deduped.add(event_id)
            at_ms = int(row.get("at_ms") or 0)
            row_key = str(row.get("storyline_key") or "")
            row_symbols = _row_symbols(row)
            score = similarity(candidate_title, str(row.get("comparison_title") or ""))
            fact_similarity = score if score >= TOLD_FACT_SIMILARITY_MIN else 0.0
            tier: ToldTier
            if row_key and row_key == storyline_key:
                tier = "storyline"
            elif candidate_symbols and candidate_symbols & row_symbols:
                tier = "asset_overlap"
            elif fact_similarity:
                tier = "fact_similarity"
            else:
                tier = "recency"
            ranked.append((TOLD_TIER_ORDER.index(tier), -fact_similarity, -at_ms, event_id, row, tier, fact_similarity))
        ranked.sort(key=lambda item: item[:4])
        chosen = _take_with_tier_caps(ranked, limit=bounded)

        return cls(
            storyline_key=storyline_key,
            source_count=len(deduped),
            entries=tuple(
                ToldLedgerEntry(
                    i=index,
                    event_id=str(row.get("event_id") or ""),
                    at_ms=int(row.get("at_ms") or 0),
                    ago_min=max(0, int(now_ms) - int(row.get("at_ms") or 0)) // 60_000,
                    storyline_key=str(row.get("storyline_key") or ""),
                    event_type=str(row.get("event_type") or ""),
                    symbols=tuple(sorted(_row_symbols(row)))[:TOLD_SYMBOLS_MAX],
                    magnitude=int(row.get("magnitude") or row.get("m") or 0),
                    direction=str(row.get("direction") or row.get("dir") or ""),
                    headline_zh=str(row.get("headline_zh") or "")[:60],
                    tier=tier,
                    similarity=round(fact_similarity, 4),
                )
                for index, (_, _, _, _, row, tier, fact_similarity) in enumerate(chosen)
            ),
        )


class _ModelVisibleEvent(_ExactContractModel):
    source: str
    strategies: tuple[str, ...] = Field(max_length=STRATEGIES_MAX)
    engine_type: str
    title: str = Field(max_length=600)
    raw_first_line: str = Field(max_length=300)
    content: str = Field(max_length=600)
    published_at_ms: int = Field(ge=0)
    member_count: int = Field(ge=1)
    family: str
    provider_score: int | None
    provider_coins: tuple[str, ...] = Field(max_length=10)
    priority: str


class _ModelVisibleGate(_ExactContractModel):
    asset_class: str
    grounded_assets: tuple[str, ...] = Field(max_length=GROUNDED_ASSETS_MAX)
    macro_lexicon: bool
    pr_template: bool
    watchlist: tuple[str, ...] = Field(max_length=WATCHLIST_MAX)


class _ModelVisibleToldEntry(_ExactContractModel):
    i: int = Field(ge=0)
    ago_min: int = Field(ge=0)
    key: str
    type: str
    sym: tuple[str, ...] = Field(max_length=TOLD_SYMBOLS_MAX)
    m: int = Field(ge=0, le=3)
    dir: str
    headline_zh: str = Field(max_length=60)


class _ModelVisibleEventStatus(_ExactContractModel):
    storyline_key: str
    preliminary: bool
    queue_lag_s: int = Field(ge=0)
    told: tuple[_ModelVisibleToldEntry, ...] = Field(max_length=TOLD_MAX)


class ModelVisibleSemanticsInput(_ExactContractModel):
    """Exact bounded JSON shape visible to ``EventSemantics``: current evidence plus the selected ledger."""

    event: _ModelVisibleEvent
    gate: _ModelVisibleGate
    event_status: _ModelVisibleEventStatus


class ModelVisibleCardInput(_ExactContractModel):
    """Exact bounded JSON shape visible to ``ReaderCard``.

    There is no ``event_status`` field and no place to put one: novelty is ``EventSemantics``' job, and a copy
    step that can re-read old cards can re-interpret them.  The boundary is the schema, not a prompt reminder.
    """

    event: _ModelVisibleEvent
    gate: _ModelVisibleGate


class TriageContext(_ExactContractModel):
    """One immutable question at the semantic-judgment Seam."""

    evidence: FrozenEventEvidence
    gate: SemanticGateContext
    watchlist: tuple[str, ...] = Field(default=(), max_length=WATCHLIST_MAX)
    told: ToldLedgerSnapshot
    now_ms: int = Field(ge=0)
    queue_lag_ms: int = Field(default=0, ge=0)

    @classmethod
    def from_card(
        cls,
        card: Mapping[str, Any],
        *,
        watchlist: Sequence[str],
        told_rows: Sequence[Mapping[str, Any]],
        now_ms: int,
        queue_lag_ms: int,
    ) -> TriageContext:
        metadata = dict(card.get("provider_metadata") or {})
        coins = tuple(
            f"{coin.get('symbol')}:{coin.get('grade') or '-'}"
            for coin in metadata.get("coins") or ()
            if isinstance(coin, Mapping) and coin.get("symbol")
        )[:10]
        storyline_key = str(card.get("storyline_key") or "")
        return cls(
            evidence=FrozenEventEvidence(
                event_id=str(card.get("event_id") or ""),
                evidence_version=int(card.get("evidence_version") or 0),
                evidence_sha256=str(card.get("evidence_sha256") or ""),
                focus_fact_id=str(card.get("focus_fact_id") or ""),
                source=str(card.get("reporting_origin") or ""),
                strategies=tuple(str(value) for value in card.get("provenance") or ())[:STRATEGIES_MAX],
                engine_type=str(card.get("engine_type") or "unknown"),
                title=str(card.get("leader_title") or "")[:600],
                raw_first_line=str(card.get("raw_first_line") or "")[:300],
                content=str(card.get("leader_description") or "")[:600],
                published_at_ms=int(card.get("opened_at_ms") or card.get("published_at_ms") or 0),
                member_count=max(1, int(card.get("member_count") or 1)),
                family=str(card.get("family") or "general"),
                provider_score=card.get("provider_score_max"),
                provider_coins=coins,
                priority=str(card.get("priority") or "normal"),
                comparison_title=str(card.get("comparison_title") or "")[:600],
            ),
            gate=SemanticGateContext(
                asset_class=str(card.get("asset_class") or "none"),
                grounded_assets=tuple(str(value) for value in card.get("grounded_assets") or ())[:GROUNDED_ASSETS_MAX],
                macro_lexicon=bool(card.get("macro_lexicon")),
                pr_template=bool(card.get("pr_template"))
                or str(card.get("admission") or "").startswith("suppressed_pr"),
            ),
            watchlist=tuple(str(value) for value in watchlist)[:WATCHLIST_MAX],
            told=ToldLedgerSnapshot.select(
                told_rows,
                now_ms=now_ms,
                storyline_key=storyline_key,
                symbols=tuple(str(value) for value in card.get("grounded_assets") or ()),
                comparison_title=str(card.get("comparison_title") or ""),
                exclude_event_id=str(card.get("event_id") or ""),
            ),
            now_ms=int(now_ms),
            queue_lag_ms=max(0, int(queue_lag_ms)),
        )

    def _visible_event(self) -> _ModelVisibleEvent:
        event = self.evidence
        return _ModelVisibleEvent(
            source=event.source,
            strategies=event.strategies,
            engine_type=event.engine_type,
            title=event.title,
            raw_first_line=event.raw_first_line,
            content=event.content,
            published_at_ms=event.published_at_ms,
            member_count=event.member_count,
            family=event.family,
            provider_score=event.provider_score,
            provider_coins=event.provider_coins,
            priority=event.priority,
        )

    def _visible_gate(self) -> _ModelVisibleGate:
        return _ModelVisibleGate(
            asset_class=self.gate.asset_class,
            grounded_assets=self.gate.grounded_assets,
            macro_lexicon=self.gate.macro_lexicon,
            pr_template=self.gate.pr_template,
            watchlist=self.watchlist,
        )

    def event_semantics_payload(self) -> dict[str, Any]:
        """Bounded evidence plus the selected told context, with audit-only ids removed."""

        return ModelVisibleSemanticsInput(
            event=self._visible_event(),
            gate=self._visible_gate(),
            event_status=_ModelVisibleEventStatus(
                storyline_key=self.told.storyline_key,
                preliminary=self.told.preliminary,
                queue_lag_s=self.queue_lag_ms // 1000,
                told=tuple(
                    _ModelVisibleToldEntry(
                        i=entry.i,
                        ago_min=entry.ago_min,
                        key=entry.storyline_key,
                        type=entry.event_type,
                        sym=entry.symbols,
                        m=entry.magnitude,
                        dir=entry.direction,
                        headline_zh=entry.headline_zh,
                    )
                    for entry in self.told.entries
                ),
            ),
        ).model_dump(mode="json")

    def reader_card_payload(self) -> dict[str, Any]:
        """Bounded evidence only. The card is written from what this Event says, not from what was told."""

        return ModelVisibleCardInput(event=self._visible_event(), gate=self._visible_gate()).model_dump(mode="json")

    def selected_context_sha256(self) -> str:
        """Identity of exactly what the model was shown. Audit and replay identity."""

        return canonical_sha(self.event_semantics_payload()["event_status"]["told"])

    def novelty_context_sha256(self) -> str:
        """Identity of the shown rows that are *evidence about this candidate*, which is what a re-ask is for.

        The recency tier is filler: it is there so a sparse candidate still sees what the reader has been
        reading, and a card at the top of it cannot turn this Event into a restatement of anything. Hashing it
        too would put the whole selection back under "any delivery invalidates the judgment", which is the rule
        this replaced. A card that joins on storyline, instrument or same-fact similarity does change the
        question, and does earn the second execution.
        """

        return canonical_sha(
            [
                {
                    "key": entry.storyline_key,
                    "type": entry.event_type,
                    "sym": list(entry.symbols),
                    "m": entry.magnitude,
                    "dir": entry.direction,
                    "headline_zh": entry.headline_zh,
                    "tier": entry.tier,
                }
                for entry in self.told.entries
                if entry.tier != "recency"
            ]
        )


class ProgramNormalizationTrace(_ExactContractModel):
    normalizer_id: Literal["semantic_normalizer_v1"] = "semantic_normalizer_v1"
    field: Literal["restates"] = "restates"
    reason: Literal["non_restatement_index_ignored"] = "non_restatement_index_ignored"
    input_value: int = Field(ge=0)
    output_value: Literal[-1] = -1


class ProgramCallTrace(_ExactContractModel):
    predictor: Literal["event_semantics", "reader_card"]
    route: Literal["primary", "fallback"]
    attempt: int
    request_sha256: str
    input_sha256: str
    signature_sha256: str
    instruction_sha256: str
    demos_sha256: str
    model_binding: str
    physical_provider_call: bool = False
    runtime_provider: str | None = None
    runtime_model: str | None = None
    runtime_model_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    runtime_binding_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    upstream_sha256: str | None = None
    output_sha256: str | None = None
    validated_output: dict[str, Any] | None = None
    normalizations: tuple[ProgramNormalizationTrace, ...] = Field(default=(), max_length=1)
    provider: str | None = None
    model: str | None = None
    model_sha256: str | None = None
    latency_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    total_tokens: int = 0
    provider_cost_microusd: int | None = None
    finish_reason: str | None = None
    error_code: str | None = None

    @model_validator(mode="after")
    def _synthetic_entry_has_no_provider_usage(self) -> ProgramCallTrace:
        if not self.physical_provider_call and (
            self.runtime_provider is not None
            or self.runtime_model is not None
            or self.runtime_model_sha256 is not None
            or self.runtime_binding_sha256 is not None
            or self.provider is not None
            or self.model is not None
            or self.model_sha256 is not None
            or self.output_sha256 is not None
            or self.validated_output is not None
            or bool(self.normalizations)
            or self.latency_ms != 0
            or self.input_tokens != 0
            or self.output_tokens != 0
            or self.cached_tokens != 0
            or self.total_tokens != 0
            or self.provider_cost_microusd is not None
            or self.finish_reason is not None
        ):
            raise ValueError("news_program_synthetic_call_provider_usage_invalid")
        return self


class ProgramTrace(_ExactContractModel):
    program_version: str
    program_sha256: str
    context_sha256: str
    factory_id: str
    topology_sha256: str
    adapter_sha256: str
    assembler_sha256: str
    event_semantics_sha256: str | None = None
    reader_card_sha256: str | None = None
    verdict_sha256: str | None = None
    answering_route: Literal["primary", "fallback"] | None = None
    fallback_from: str | None = None
    novelty_defaulted: bool = False
    calls: tuple[ProgramCallTrace, ...] = ()


class ProgramUsage(_ExactContractModel):
    wall_latency_ms: int = Field(ge=0)
    call_count: int = Field(ge=0, le=6)
    physical_call_count: int = Field(default=0, ge=0, le=6)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cached_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    provider_cost_microusd: int | None = Field(default=None, ge=0)

    @property
    def provider_cost_usd(self) -> float | None:
        return None if self.provider_cost_microusd is None else self.provider_cost_microusd / 1_000_000


def aggregate_program_usage(calls: Sequence[ProgramCallTrace]) -> dict[str, Any]:
    physical_calls = [call for call in calls if call.physical_provider_call]
    complete_cost = bool(physical_calls) and all(call.provider_cost_microusd is not None for call in physical_calls)
    return {
        "call_count": len(calls),
        "physical_call_count": len(physical_calls),
        "input_tokens": sum(call.input_tokens for call in physical_calls),
        "output_tokens": sum(call.output_tokens for call in physical_calls),
        "cached_tokens": sum(call.cached_tokens for call in physical_calls),
        "total_tokens": sum(call.total_tokens for call in physical_calls),
        "provider_cost_microusd": (
            sum(cast(int, call.provider_cost_microusd) for call in physical_calls) if complete_cost else None
        ),
    }


class SemanticJudgment(_ExactContractModel):
    verdict: TriageVerdict
    program_version: str
    program_sha256: str
    trace: ProgramTrace
    usage: ProgramUsage
    answering_model: str | None = None
    fallback_from: str | None = None

    @model_validator(mode="after")
    def _trace_and_usage_match_judgment(self) -> SemanticJudgment:
        if (
            self.program_version != self.trace.program_version
            or self.program_sha256 != self.trace.program_sha256
            or self.fallback_from != self.trace.fallback_from
            or self.trace.verdict_sha256 != canonical_sha(self.verdict.model_dump(mode="json"))
        ):
            raise ValueError("news_program_judgment_trace_identity_mismatch")
        expected_usage = aggregate_program_usage(self.trace.calls)
        actual_usage = self.usage.model_dump(mode="json", exclude={"wall_latency_ms"})
        if actual_usage != expected_usage:
            raise ValueError("news_program_judgment_usage_mismatch")
        return self


class SemanticJudgeError(Exception):
    """Declared failure mode of the semantic-judgment Interface."""

    def __init__(
        self,
        code: str,
        *,
        retryable: bool,
        output_failure: bool,
        attempts: int,
        partial_trace: ProgramTrace | None,
        finish_reason: str | None = None,
        failing_predictor: str | None = None,
        primary_code: str | None = None,
    ) -> None:
        self.code = code
        self.retryable = retryable
        self.output_failure = output_failure
        self.attempts = attempts
        self.partial_trace = partial_trace
        self.finish_reason = finish_reason
        self.failing_predictor = failing_predictor
        self.primary_code = primary_code
        super().__init__(code)


@runtime_checkable
class SemanticJudge(Protocol):
    async def judge(self, context: TriageContext) -> SemanticJudgment: ...


__all__ = [
    "GROUNDED_ASSETS_MAX",
    "STRATEGIES_MAX",
    "TOLD_MAX",
    "TOLD_SELECTOR_ID",
    "TOLD_SELECTOR_SHA256",
    "TOLD_SOURCE_MAX",
    "TOLD_STORYLINE_TIER_MAX",
    "TOLD_WINDOW_MS",
    "WATCHLIST_MAX",
    "FrozenEventEvidence",
    "ModelVisibleCardInput",
    "ModelVisibleSemanticsInput",
    "ProgramCallTrace",
    "ProgramNormalizationTrace",
    "ProgramTrace",
    "ProgramUsage",
    "SemanticGateContext",
    "SemanticJudge",
    "SemanticJudgeError",
    "SemanticJudgment",
    "ToldLedgerEntry",
    "ToldLedgerSnapshot",
    "TriageContext",
    "aggregate_program_usage",
]
