"""Framework-neutral public Interface for News semantic judgment.

Callers construct one immutable :class:`TriageContext` and invoke one
``SemanticJudge.judge`` method.  DSPy, Predictor state, Program artifacts,
model routes and compiler state are deliberately absent from this module.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final, Literal, Protocol, cast, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..artifact_identity import canonical_sha
from ..models import TriageAsset, TriageVerdict
from ..told_context import TOLD_MAX as _TOLD_MAX
from ..told_context import TOLD_SYMBOLS_MAX as _TOLD_SYMBOLS_MAX
from ..told_context import ToldLedgerSnapshot as _ToldLedgerSnapshot

# 16, not 12. Measured on every accepted restatement whose duplicate target was inside the 4 h ledger (n=22),
# target recall@N: 12 rows recalls 19, 16 rows recalls 21. The binding constraint on the old selector was never
# the ranking — a dense storyline puts 18-22 genuinely related cards in one window — so no ordering recovers
# what the cap excludes. ReaderCard no longer receives the ledger at all, which more than pays for the four
# extra rows: the two-call total moves ~+2%.
# No tier may take every slot. Ranking storyline first and filling the rest by tier order scored *below* the
# predecessor (18/22 against 19/22): with 14-17 same-storyline cards in the window, tier 1 consumed all 16 rows
# and the shared-instrument evidence that actually held the duplicate never got one. Capping the storyline tier
# is what makes the lower tiers reachable.
# Retrieval's own threshold on comparison titles, deliberately not `news.policy.similarity_max`: that knob is
# operator-owned duplicate policy over reader headlines, and coupling the two would let a policy edit silently
# change what the model is allowed to see.
WATCHLIST_MAX: Final[int] = 64
GROUNDED_ASSETS_MAX: Final[int] = 16
STRATEGIES_MAX: Final[int] = 16
TRADE_CODE_SET_MAX: Final[int] = 4

TradeImpactBreadth = Literal[
    "none",
    "single_instrument",
    "sector",
    "regional",
    "cross_asset",
    "global_systemic",
]
TradeTradability = Literal["direct", "second_order", "contextual", "none"]
TradeSurprise = Literal["unscheduled", "material_vs_expectation", "in_line", "unknown"]
TradeDevelopmentDelta = Literal["state_change", "material_detail", "color_only", "scheduled"]
# `product_progress` (#173): a first-party product/protocol/market capability reaching a verifiable new state
# had no true channel — `exchange_access` is only who may trade, `earnings_cashflow` only the money mechanism —
# so it came back empty, and empty channels may co-exist only with contextual/none + background/none, which
# structurally held every product event. Never brand marketing, a roadmap, or a cumulative vanity count.
TradeChannel = Literal[
    "rates",
    "liquidity",
    "risk_premium",
    "energy_supply",
    "commodity_supply",
    "commodity_demand",
    "regulation",
    "exchange_access",
    "product_progress",
    "earnings_cashflow",
    "positioning_flow",
    "security_incident",
]
TradeAffectedMarket = Literal[
    "crypto_broad",
    "us_equity_broad",
    "rates",
    "fx",
    "energy",
    "metals",
    "single_asset",
]
ReaderValue = Literal["escalate", "realtime", "background", "none"]

TRADE_CHANNEL_ORDER: Final[tuple[TradeChannel, ...]] = (
    "rates",
    "liquidity",
    "risk_premium",
    "energy_supply",
    "commodity_supply",
    "commodity_demand",
    "regulation",
    "exchange_access",
    "product_progress",
    "earnings_cashflow",
    "positioning_flow",
    "security_incident",
)
TRADE_AFFECTED_MARKET_ORDER: Final[tuple[TradeAffectedMarket, ...]] = (
    "crypto_broad",
    "us_equity_broad",
    "rates",
    "fx",
    "energy",
    "metals",
    "single_asset",
)
EDITORIAL_CONTRACT_VERSION: Final[str] = "news_editorial_v1"


class _ExactContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _canonical_code_set(value: Any, *, order: Sequence[str]) -> Any:
    """Canonicalize a bounded model-emitted set before Pydantic applies its max-length constraint."""

    if not isinstance(value, (list, tuple)):
        return value
    if not all(isinstance(item, str) and item in order for item in value):
        # Preserve an invalid value for the typed Literal validator.  Silently
        # dropping an unknown code would turn schema-invalid model output into
        # a different, apparently valid editorial judgment.
        return value
    present = set(value)
    return tuple(item for item in order if item in present)


class TradeRelevanceV1(_ExactContractModel):
    """Typed editorial relevance owned by ``EventSemantics.v2``.

    ``channels`` and ``affected_markets`` are sets on the wire but tuples in the
    contract.  Canonical code-owned ordering makes exact gold, hashing and replay
    independent of the order in which a model emitted them.
    """

    impact_breadth: TradeImpactBreadth
    tradability: TradeTradability
    surprise: TradeSurprise
    development_delta: TradeDevelopmentDelta
    channels: tuple[TradeChannel, ...] = Field(default=(), max_length=TRADE_CODE_SET_MAX)
    affected_markets: tuple[TradeAffectedMarket, ...] = Field(default=(), max_length=TRADE_CODE_SET_MAX)
    reader_value: ReaderValue

    @field_validator("channels", mode="before")
    @classmethod
    def _canonical_channels(cls, value: Any) -> Any:
        return _canonical_code_set(value, order=TRADE_CHANNEL_ORDER)

    @field_validator("affected_markets", mode="before")
    @classmethod
    def _canonical_markets(cls, value: Any) -> Any:
        return _canonical_code_set(value, order=TRADE_AFFECTED_MARKET_ORDER)

    @model_validator(mode="after")
    def _empty_surfaces_are_background_only(self) -> TradeRelevanceV1:
        if (not self.channels or not self.affected_markets) and not (
            self.tradability in {"contextual", "none"} and self.reader_value in {"background", "none"}
        ):
            raise ValueError("news_trade_relevance_empty_surface_invalid")
        return self


class ReaderCardSemanticView(_ExactContractModel):
    """The complete semantic Interface visible to ``ReaderCard``.

    It intentionally excludes model delivery intent, surprise, tradability and
    development state.  ReaderCard writes factual copy; it does not get a second
    opportunity to infer urgency or final action.
    """

    event_type: str
    assets: tuple[TriageAsset, ...] = Field(default=(), max_length=8)
    direction: Literal["bullish", "bearish", "neutral", "unclear"]
    magnitude: int = Field(ge=0, le=3)
    novelty: Literal["new_fact", "progression", "restatement"]
    restates: int = Field(default=-1, ge=-1)
    scope: Literal["macro", "sector", "single_name"]
    channels: tuple[TradeChannel, ...] = Field(default=(), max_length=TRADE_CODE_SET_MAX)
    affected_markets: tuple[TradeAffectedMarket, ...] = Field(default=(), max_length=TRADE_CODE_SET_MAX)


class EditorialEnvelope(_ExactContractModel):
    """Versioned editorial sibling persisted atomically with one verdict."""

    editorial_contract_version: Literal["news_editorial_v1"] = "news_editorial_v1"
    editorial_origin: Literal["model", "telemetry_deterministic", "degraded_unavailable"]
    relevance: TradeRelevanceV1 | None
    editorial_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @classmethod
    def issue(
        cls,
        *,
        editorial_origin: Literal["model", "telemetry_deterministic", "degraded_unavailable"],
        relevance: TradeRelevanceV1 | None,
    ) -> EditorialEnvelope:
        payload = {
            "editorial_contract_version": EDITORIAL_CONTRACT_VERSION,
            "editorial_origin": editorial_origin,
            "relevance": None if relevance is None else relevance.model_dump(mode="json"),
        }
        return cls(**payload, editorial_sha256=canonical_sha(payload))

    @model_validator(mode="after")
    def _origin_and_identity_are_exact(self) -> EditorialEnvelope:
        if (self.editorial_origin == "model") != (self.relevance is not None):
            raise ValueError("news_editorial_origin_relevance_mismatch")
        payload = self.model_dump(mode="json", exclude={"editorial_sha256"})
        if self.editorial_sha256 != canonical_sha(payload):
            raise ValueError("news_editorial_hash_mismatch")
        return self


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
    queue_priority: Literal["high", "normal"] = "normal"
    # Selector input only, never rendered: the Deduper's normalized title, which is what a same-fact
    # comparison against a prior Event is actually made of.  The model reads `title`.
    comparison_title: str = Field(default="", max_length=600)


class SemanticGateContext(_ExactContractModel):
    asset_class: str = "none"
    grounded_assets: tuple[str, ...] = Field(default=(), max_length=GROUNDED_ASSETS_MAX)
    macro_lexicon: bool = False
    pr_template: bool = False


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
    provider_coins: tuple[str, ...] = Field(max_length=10)


class _ModelVisibleGate(_ExactContractModel):
    asset_class: str
    grounded_assets: tuple[str, ...] = Field(max_length=GROUNDED_ASSETS_MAX)
    pr_template: bool


class _ModelVisibleToldEntry(_ExactContractModel):
    i: int = Field(ge=0)
    ago_min: int = Field(ge=0)
    key: str
    type: str
    sym: tuple[str, ...] = Field(max_length=_TOLD_SYMBOLS_MAX)
    m: int = Field(ge=0, le=3)
    dir: str
    headline_zh: str = Field(max_length=60)


class _ModelVisibleEventStatus(_ExactContractModel):
    storyline_key: str
    preliminary: bool
    told: tuple[_ModelVisibleToldEntry, ...] = Field(max_length=_TOLD_MAX)


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
    told: _ToldLedgerSnapshot
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
                queue_priority=str(card.get("queue_priority") or "normal"),
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
            told=_ToldLedgerSnapshot.select(
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
            provider_coins=event.provider_coins,
        )

    def _visible_gate(self) -> _ModelVisibleGate:
        return _ModelVisibleGate(
            asset_class=self.gate.asset_class,
            grounded_assets=self.gate.grounded_assets,
            pr_template=self.gate.pr_template,
        )

    def event_semantics_payload(self) -> dict[str, Any]:
        """Bounded evidence plus the selected told context, with audit-only ids removed."""

        return ModelVisibleSemanticsInput(
            event=self._visible_event(),
            gate=self._visible_gate(),
            event_status=_ModelVisibleEventStatus(
                storyline_key=self.told.storyline_key,
                preliminary=self.told.preliminary,
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
    normalizer_id: Literal["semantic_normalizer_v2"] = "semantic_normalizer_v2"
    field: Literal["restates", "channels", "affected_markets"]
    reason: Literal["non_restatement_index_ignored", "canonical_set_order"]
    input_value: int | tuple[str, ...]
    output_value: int | tuple[str, ...]

    @model_validator(mode="after")
    def _field_and_values_match_reason(self) -> ProgramNormalizationTrace:
        if self.field == "restates":
            if (
                self.reason != "non_restatement_index_ignored"
                or not isinstance(self.input_value, int)
                or self.input_value < 0
                or self.output_value != -1
            ):
                raise ValueError("news_program_restatement_normalization_invalid")
            return self
        if (
            self.reason != "canonical_set_order"
            or not isinstance(self.input_value, tuple)
            or not isinstance(self.output_value, tuple)
        ):
            raise ValueError("news_program_relevance_normalization_invalid")
        return self


class ProgramCallTrace(_ExactContractModel):
    predictor: Literal["event_semantics", "reader_card"]
    route: Literal["primary", "fallback"]
    attempt: int
    request_sha256: str
    input_sha256: str
    model_binding: str
    physical_provider_call: bool = False
    runtime_provider: str | None = None
    runtime_model: str | None = None
    runtime_model_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    runtime_binding_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    upstream_sha256: str | None = None
    output_sha256: str | None = None
    validated_output: dict[str, Any] | None = None
    normalizations: tuple[ProgramNormalizationTrace, ...] = Field(default=(), max_length=3)
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
    # Bounded, secret-scrubbed provider error body for a refused request (#310). Absent on every
    # pre-factory_v9 trace and on any attempt the provider answered.
    error_detail: str | None = None

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
            or self.error_detail is not None
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
    event_semantics_sha256: str | None = None
    reader_card_sha256: str | None = None
    verdict_sha256: str | None = None
    editorial_sha256: str | None = None
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


class ScoredJudgment(_ExactContractModel):
    """Canonical verdict/editorial projection shared by every learning surface."""

    verdict: TriageVerdict
    editorial: EditorialEnvelope
    verdict_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scored_judgment_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @classmethod
    def issue(cls, *, verdict: TriageVerdict, editorial: EditorialEnvelope) -> ScoredJudgment:
        verdict_sha256 = canonical_sha(verdict.model_dump(mode="json"))
        payload = {
            "verdict": verdict.model_dump(mode="json"),
            "editorial": editorial.model_dump(mode="json"),
            "verdict_sha256": verdict_sha256,
        }
        return cls(
            verdict=verdict,
            editorial=editorial,
            verdict_sha256=verdict_sha256,
            scored_judgment_sha256=canonical_sha(payload),
        )

    @model_validator(mode="after")
    def _projection_identity_is_exact(self) -> ScoredJudgment:
        expected_verdict = canonical_sha(self.verdict.model_dump(mode="json"))
        payload = self.model_dump(mode="json", exclude={"scored_judgment_sha256"})
        if self.verdict_sha256 != expected_verdict or self.scored_judgment_sha256 != canonical_sha(payload):
            raise ValueError("news_scored_judgment_identity_mismatch")
        return self


class SemanticJudgment(_ExactContractModel):
    verdict: TriageVerdict
    editorial: EditorialEnvelope
    program_version: str
    program_sha256: str
    trace: ProgramTrace
    usage: ProgramUsage
    answering_model: str | None = None
    fallback_from: str | None = None

    def scored(self) -> ScoredJudgment:
        return ScoredJudgment.issue(verdict=self.verdict, editorial=self.editorial)

    @model_validator(mode="after")
    def _trace_and_usage_match_judgment(self) -> SemanticJudgment:
        if (
            self.program_version != self.trace.program_version
            or self.program_sha256 != self.trace.program_sha256
            or self.fallback_from != self.trace.fallback_from
            or self.trace.verdict_sha256 != canonical_sha(self.verdict.model_dump(mode="json"))
            or self.trace.editorial_sha256 != self.editorial.editorial_sha256
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
    "EDITORIAL_CONTRACT_VERSION",
    "GROUNDED_ASSETS_MAX",
    "STRATEGIES_MAX",
    "TRADE_AFFECTED_MARKET_ORDER",
    "TRADE_CHANNEL_ORDER",
    "WATCHLIST_MAX",
    "EditorialEnvelope",
    "FrozenEventEvidence",
    "ModelVisibleCardInput",
    "ModelVisibleSemanticsInput",
    "ProgramCallTrace",
    "ProgramNormalizationTrace",
    "ProgramTrace",
    "ProgramUsage",
    "ReaderCardSemanticView",
    "ScoredJudgment",
    "SemanticGateContext",
    "SemanticJudge",
    "SemanticJudgeError",
    "SemanticJudgment",
    "TradeRelevanceV1",
    "TriageContext",
    "aggregate_program_usage",
]
