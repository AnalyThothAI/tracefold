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
from .models import TriageVerdict

TOLD_WINDOW_MS: Final[int] = 4 * 3_600_000
TOLD_MAX: Final[int] = 12
TOLD_SAME_KEY_MAX: Final[int] = 6
WATCHLIST_MAX: Final[int] = 64
GROUNDED_ASSETS_MAX: Final[int] = 16
STRATEGIES_MAX: Final[int] = 16


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


class SemanticGateContext(_ExactContractModel):
    asset_class: str = "none"
    grounded_assets: tuple[str, ...] = Field(default=(), max_length=GROUNDED_ASSETS_MAX)
    macro_lexicon: bool = False
    pr_template: bool = False


class ToldLedgerEntry(_ExactContractModel):
    i: int = Field(ge=0)
    event_id: str
    at_ms: int = Field(ge=0)
    ago_min: int = Field(ge=0)
    magnitude: int = Field(ge=0, le=3)
    direction: str
    headline_zh: str = Field(max_length=60)


class ToldLedgerSnapshot(_ExactContractModel):
    storyline_key: str
    preliminary: bool = True
    entries: tuple[ToldLedgerEntry, ...] = Field(default=(), max_length=TOLD_MAX)

    @classmethod
    def from_rows(
        cls,
        rows: Sequence[Mapping[str, Any]],
        *,
        now_ms: int,
        storyline_key: str,
        limit: int = TOLD_MAX,
    ) -> ToldLedgerSnapshot:
        """Select the exact reader ledger shown to both Predictors."""

        bounded = max(0, min(int(limit), TOLD_MAX))
        cutoff = int(now_ms) - TOLD_WINDOW_MS
        ordered = sorted(
            (row for row in rows if int(row.get("at_ms") or 0) >= cutoff),
            key=lambda row: -int(row.get("at_ms") or 0),
        )
        same = [row for row in ordered if str(row.get("storyline_key") or "") == storyline_key]
        others = [row for row in ordered if str(row.get("storyline_key") or "") != storyline_key]
        chosen = same[: min(TOLD_SAME_KEY_MAX, bounded)]
        chosen += others[: max(0, bounded - len(chosen))]
        chosen += same[TOLD_SAME_KEY_MAX:][: max(0, bounded - len(chosen))]
        chosen.sort(key=lambda row: -int(row.get("at_ms") or 0))
        return cls(
            storyline_key=storyline_key,
            entries=tuple(
                ToldLedgerEntry(
                    i=index,
                    event_id=str(row.get("event_id") or ""),
                    at_ms=int(row.get("at_ms") or 0),
                    ago_min=max(0, int(now_ms) - int(row.get("at_ms") or 0)) // 60_000,
                    magnitude=int(row.get("magnitude") or row.get("m") or 0),
                    direction=str(row.get("direction") or row.get("dir") or ""),
                    headline_zh=str(row.get("headline_zh") or "")[:60],
                )
                for index, row in enumerate(chosen[:bounded])
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
    m: int = Field(ge=0, le=3)
    dir: str
    headline_zh: str = Field(max_length=60)


class _ModelVisibleEventStatus(_ExactContractModel):
    storyline_key: str
    preliminary: bool
    queue_lag_s: int = Field(ge=0)
    told: tuple[_ModelVisibleToldEntry, ...] = Field(max_length=TOLD_MAX)


class ModelVisibleTriageInput(_ExactContractModel):
    """Exact bounded JSON shape visible to either Predictor."""

    event: _ModelVisibleEvent
    gate: _ModelVisibleGate
    event_status: _ModelVisibleEventStatus


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
            ),
            gate=SemanticGateContext(
                asset_class=str(card.get("asset_class") or "none"),
                grounded_assets=tuple(str(value) for value in card.get("grounded_assets") or ())[:GROUNDED_ASSETS_MAX],
                macro_lexicon=bool(card.get("macro_lexicon")),
                pr_template=bool(card.get("pr_template"))
                or str(card.get("admission") or "").startswith("suppressed_pr"),
            ),
            watchlist=tuple(str(value) for value in watchlist)[:WATCHLIST_MAX],
            told=ToldLedgerSnapshot.from_rows(told_rows, now_ms=now_ms, storyline_key=storyline_key),
            now_ms=int(now_ms),
            queue_lag_ms=max(0, int(queue_lag_ms)),
        )

    def model_payload(self) -> dict[str, Any]:
        """Return bounded model-visible evidence with audit-only ids removed."""

        event = self.evidence
        return ModelVisibleTriageInput(
            event=_ModelVisibleEvent(
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
            ),
            gate=_ModelVisibleGate(
                asset_class=self.gate.asset_class,
                grounded_assets=self.gate.grounded_assets,
                macro_lexicon=self.gate.macro_lexicon,
                pr_template=self.gate.pr_template,
                watchlist=self.watchlist,
            ),
            event_status=_ModelVisibleEventStatus(
                storyline_key=self.told.storyline_key,
                preliminary=self.told.preliminary,
                queue_lag_s=self.queue_lag_ms // 1000,
                told=tuple(
                    _ModelVisibleToldEntry(
                        i=entry.i,
                        ago_min=entry.ago_min,
                        m=entry.magnitude,
                        dir=entry.direction,
                        headline_zh=entry.headline_zh,
                    )
                    for entry in self.told.entries
                ),
            ),
        ).model_dump(mode="json")


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
    "TOLD_SAME_KEY_MAX",
    "TOLD_WINDOW_MS",
    "WATCHLIST_MAX",
    "FrozenEventEvidence",
    "ModelVisibleTriageInput",
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
