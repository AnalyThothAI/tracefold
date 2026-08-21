"""Program-native semantic judgment for News V3.

The public Interface is deliberately small: callers submit one immutable
``TriageContext`` to ``SemanticJudge.judge`` and receive one complete
``SemanticJudgment``.  The hidden DSPy graph is exactly
``EventSemantics -> ReaderCard -> VerdictAssembler``.  Model transport is an
Adapter Seam; domain validation, retry/fallback budgets, identity and audit
remain owned by this Module.

Only canonical JSON state is loadable.  This module never loads pickle,
cloudpickle, DSPy Flex state, arbitrary classes, endpoints, or credentials.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata
import importlib.resources
import json
import math
import re
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Final, Literal, Protocol, TypeVar, cast, runtime_checkable

import dspy  # type: ignore[import-untyped]
from dspy.adapters.chat_adapter import ChatAdapter  # type: ignore[import-untyped]
from dspy.adapters.json_adapter import _get_structured_outputs_response_format  # type: ignore[import-untyped]
from dspy.clients.openai_format import (  # type: ignore[import-untyped]
    completion_to_lm_response,
    cost_from_response,
    responses_to_lm_response,
    usage_from_response,
)
from dspy.core.types import LMRequest, LMResponse  # type: ignore[import-untyped]
from dspy.utils.exceptions import AdapterParseError  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from ..artifact_identity import canonical_json, canonical_sha
from ..models import TriageAsset, TriageVerdict

TOLD_WINDOW_MS: Final[int] = 4 * 3_600_000
TOLD_MAX: Final[int] = 12
TOLD_SAME_KEY_MAX: Final[int] = 6
WATCHLIST_MAX: Final[int] = 64
GROUNDED_ASSETS_MAX: Final[int] = 16
STRATEGIES_MAX: Final[int] = 16
PROGRAM_DEMOS_MAX: Final[int] = 32
PROGRAM_DEMO_JSON_MAX_BYTES: Final[int] = 32_768
PROGRAM_INSTRUCTION_MAX_BYTES: Final[int] = 32_768
PROGRAM_DEPENDENCY_LOCK_SHA256: Final[str] = "defdd610578ecd1f1f667f5eaf0ebf0b94ae866b16fd5cdd41ba3fc793ab4b37"

PROGRAM_SCHEMA_VERSION: Final[str] = "news_semantic_program_artifact_v1"
PROGRAM_FACTORY_ID: Final[str] = "tracefold.news.semantic_program.factory_v1"
PROGRAM_TOPOLOGY_SHA256: Final[str] = canonical_sha(
    {"nodes": ["event_semantics", "reader_card", "verdict_assembler"], "edges": [[0, 1], [1, 2]]}
)
PROGRAM_ADAPTER_SHA256: Final[str] = canonical_sha(
    {
        "adapter": "predictor_adapter_v2",
        "cache": False,
        "history": False,
        "hidden_retry": False,
        "metadata": "exact_provider_response",
        "request_identity": "runtime_model_binding",
    }
)
PROGRAM_ASSEMBLER_SHA256: Final[str] = canonical_sha(
    {
        "assembler": "verdict_assembler_v1",
        "restatement_index": "strict",
        "title_sentinel": "empty_when_equal",
    }
)
PROGRAM_INPUT_CONTRACT_SHA256: Final[str] = canonical_sha(
    {"context": "TriageContext.v1", "model_payload": "bounded_without_audit_ids.v1"}
)
EVENT_SEMANTICS_SIGNATURE_SHA256: Final[str] = canonical_sha(
    {
        "signature": "EventSemantics.v1",
        "inputs": ["evidence_json"],
        "outputs": ["semantics"],
    }
)
READER_CARD_SIGNATURE_SHA256: Final[str] = canonical_sha(
    {
        "signature": "ReaderCard.v1",
        "inputs": ["evidence_json", "semantics_json"],
        "outputs": ["card"],
    }
)

_FORBIDDEN_STATE_KEY_PARTS: Final[frozenset[tuple[str, ...]]] = frozenset(
    {
        ("api",),
        ("auth",),
        ("authorization",),
        ("base", "url"),
        ("callback",),
        ("credential",),
        ("credentials",),
        ("endpoint",),
        ("header",),
        ("headers",),
        ("history",),
        ("model", "list"),
        ("password",),
        ("secret",),
        ("token",),
    }
)
_DEMO_FIELDS: Final[dict[str, frozenset[str]]] = {
    "event_semantics": frozenset({"evidence_json", "semantics"}),
    "reader_card": frozenset({"evidence_json", "semantics_json", "card"}),
}
_RETRYABLE_MARKERS: Final[tuple[str, ...]] = (
    "timeout",
    "connection",
    "ratelimit",
    "rate_limit",
    "serviceunavailable",
    "temporar",
    "overloaded",
)
_TRUNCATED_FINISH_REASONS: Final[frozenset[str]] = frozenset({"length", "max_tokens", "max_output_tokens"})
_MODEL_BINDING_SLOTS: Final[frozenset[str]] = frozenset(
    {
        "event_semantics.primary",
        "event_semantics.fallback",
        "reader_card.primary",
        "reader_card.fallback",
    }
)


def _runtime_factory_source_sha256() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _runtime_dependency_lock_sha256() -> str:
    """Return the lock identity carried by every installed package.

    A wheel has no repository root or ``uv.lock``.  The generated constant is
    therefore part of the trusted application package, while a drift test and
    the artifact maintenance tool require it to match the source lock exactly.
    """

    return PROGRAM_DEPENDENCY_LOCK_SHA256


class _ExactModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RuntimeModelIdentity(_ExactModel):
    """Secret-free identity of the concrete model route used for one request."""

    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    model_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    binding_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @classmethod
    def issue(
        cls,
        *,
        provider: str,
        model: str,
        model_sha256: str | None = None,
    ) -> RuntimeModelIdentity:
        normalized_provider = str(provider or "unknown").strip() or "unknown"
        normalized_model = str(model).strip()
        if not normalized_model:
            raise ValueError("news_program_runtime_model_empty")
        identity_sha = model_sha256 or canonical_sha({"provider": normalized_provider, "model": normalized_model})
        return cls(
            provider=normalized_provider,
            model=normalized_model,
            model_sha256=identity_sha,
            binding_sha256=canonical_sha(
                {
                    "provider": normalized_provider,
                    "model": normalized_model,
                    "model_sha256": identity_sha,
                }
            ),
        )

    @model_validator(mode="after")
    def _binding_matches_fields(self) -> RuntimeModelIdentity:
        expected = canonical_sha({"provider": self.provider, "model": self.model, "model_sha256": self.model_sha256})
        if self.binding_sha256 != expected:
            raise ValueError("news_program_runtime_binding_identity_mismatch")
        return self


class ExactProviderMetadata(_ExactModel):
    """Metadata normalized from exactly one DSPy 3.3 provider response."""

    response_model: str | None = None
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cached_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    provider_cost_microusd: int | None = Field(default=None, ge=0)
    finish_reason: str | None = None


class ExactProviderCallCapture:
    """Task-local capture owned by one logical DSPy Predictor invocation."""

    def __init__(self) -> None:
        self._metadata: list[ExactProviderMetadata] = []
        self._errors: list[Exception] = []

    def record(self, lm: dspy.LM, response: Any) -> None:
        try:
            self._metadata.append(_exact_provider_metadata(lm, response))
        except Exception as exc:
            self._errors.append(exc)

    def record_metadata(self, metadata: ExactProviderMetadata) -> None:
        self._metadata.append(metadata)

    def require_exactly_one(self) -> ExactProviderMetadata:
        if self._errors or len(self._metadata) != 1:
            raise PredictorAdapterError("news_program_provider_metadata_unavailable")
        return self._metadata[0]


_ACTIVE_PROVIDER_CAPTURE: ContextVar[ExactProviderCallCapture | None] = ContextVar(
    "tracefold_news_provider_capture", default=None
)


class ExactMetadataDspyLM(dspy.LM):  # type: ignore[misc]
    """DSPy LM that exposes per-call provider metadata without shared-history lookup."""

    def _process_lm_response(
        self,
        response: Any,
        prompt: str | None,
        messages: list[dict[str, Any]] | None,
        **kwargs: Any,
    ) -> Any:
        capture = _ACTIVE_PROVIDER_CAPTURE.get()
        if capture is not None:
            capture.record(self, response)
        return super()._process_lm_response(response, prompt, messages, **kwargs)

    @contextmanager
    def observe_exact_call(self) -> Iterator[ExactProviderCallCapture]:
        capture = ExactProviderCallCapture()
        token = _ACTIVE_PROVIDER_CAPTURE.set(capture)
        try:
            yield capture
        finally:
            _ACTIVE_PROVIDER_CAPTURE.reset(token)


def _exact_provider_metadata(lm: dspy.LM, response: Any) -> ExactProviderMetadata:
    if isinstance(response, LMResponse):
        normalized = response
    else:
        request = LMRequest(model=str(lm.model), messages=[])
        if str(getattr(lm, "model_type", "chat")) == "responses":
            normalized = responses_to_lm_response(response, request)
        else:
            normalized = completion_to_lm_response(response, request)
        normalized = normalized.model_copy(
            update={
                "model": getattr(response, "model", None) or normalized.model,
                "usage": usage_from_response(response),
                "cost": cost_from_response(response),
                "provider_response": response,
            }
        )
    usage = normalized.usage_as_dict()
    input_tokens = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
    total_tokens = int(usage.get("total_tokens") or input_tokens + output_tokens)
    cached_tokens = int(usage.get("cache_read_tokens") or usage.get("cache_read_input_tokens") or 0)
    for detail_key in ("prompt_tokens_details", "input_tokens_details"):
        detail = usage.get(detail_key)
        if isinstance(detail, BaseModel):
            detail = detail.model_dump()
        if isinstance(detail, Mapping):
            cached_tokens = max(cached_tokens, int(detail.get("cached_tokens") or 0))
    details = usage.get("details")
    if isinstance(details, Mapping):
        cached_tokens = max(cached_tokens, int(details.get("cached_tokens") or details.get("cache_read_tokens") or 0))
    cost_microusd = None
    if normalized.cost is not None:
        cost = Decimal(str(normalized.cost))
        if not cost.is_finite() or cost < 0:
            raise ValueError("news_program_provider_cost_invalid")
        cost_microusd = int((cost * Decimal(1_000_000)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    finish_reason = normalized.output.finish_reason
    if finish_reason is None and normalized.output.truncated:
        finish_reason = "length"
    return ExactProviderMetadata(
        response_model=normalized.model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_tokens=cached_tokens,
        total_tokens=total_tokens,
        provider_cost_microusd=cost_microusd,
        finish_reason=str(finish_reason).casefold() if finish_reason is not None else None,
    )


class FrozenEventEvidence(_ExactModel):
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


class SemanticGateContext(_ExactModel):
    asset_class: str = "none"
    grounded_assets: tuple[str, ...] = Field(default=(), max_length=GROUNDED_ASSETS_MAX)
    macro_lexicon: bool = False
    pr_template: bool = False


class ToldLedgerEntry(_ExactModel):
    i: int = Field(ge=0)
    event_id: str
    at_ms: int = Field(ge=0)
    ago_min: int = Field(ge=0)
    magnitude: int = Field(ge=0, le=3)
    direction: str
    headline_zh: str = Field(max_length=60)


class ToldLedgerSnapshot(_ExactModel):
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
        """Select the exact reader ledger shown to both Predictors.

        Six slots are reserved for the preliminary storyline, remaining slots
        preserve cross-storyline evidence, and the final visible order is
        newest-first.  Event ids stay in this snapshot for audit but are
        removed from model-visible input.
        """

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
        entries = tuple(
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
        )
        return cls(storyline_key=storyline_key, entries=entries)


class _ModelVisibleEvent(_ExactModel):
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


class _ModelVisibleGate(_ExactModel):
    asset_class: str
    grounded_assets: tuple[str, ...] = Field(max_length=GROUNDED_ASSETS_MAX)
    macro_lexicon: bool
    pr_template: bool
    watchlist: tuple[str, ...] = Field(max_length=WATCHLIST_MAX)


class _ModelVisibleToldEntry(_ExactModel):
    i: int = Field(ge=0)
    ago_min: int = Field(ge=0)
    m: int = Field(ge=0, le=3)
    dir: str
    headline_zh: str = Field(max_length=60)


class _ModelVisibleEventStatus(_ExactModel):
    storyline_key: str
    preliminary: bool
    queue_lag_s: int = Field(ge=0)
    told: tuple[_ModelVisibleToldEntry, ...] = Field(max_length=TOLD_MAX)


class _ModelVisibleTriageInput(_ExactModel):
    """The only JSON shape that an artifact demo may send to either Predictor."""

    event: _ModelVisibleEvent
    gate: _ModelVisibleGate
    event_status: _ModelVisibleEventStatus


class TriageContext(_ExactModel):
    """One immutable question for the semantic Program."""

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
        strategies = tuple(str(value) for value in card.get("provenance") or ())[:STRATEGIES_MAX]
        storyline_key = str(card.get("storyline_key") or "")
        evidence = FrozenEventEvidence(
            event_id=str(card.get("event_id") or ""),
            evidence_version=int(card.get("evidence_version") or 0),
            evidence_sha256=str(card.get("evidence_sha256") or ""),
            focus_fact_id=str(card.get("focus_fact_id") or ""),
            source=str(card.get("reporting_origin") or ""),
            strategies=strategies,
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
        )
        gate = SemanticGateContext(
            asset_class=str(card.get("asset_class") or "none"),
            grounded_assets=tuple(str(value) for value in card.get("grounded_assets") or ())[:GROUNDED_ASSETS_MAX],
            macro_lexicon=bool(card.get("macro_lexicon")),
            pr_template=bool(card.get("pr_template")) or str(card.get("admission") or "").startswith("suppressed_pr"),
        )
        return cls(
            evidence=evidence,
            gate=gate,
            watchlist=tuple(str(value) for value in watchlist)[:WATCHLIST_MAX],
            told=ToldLedgerSnapshot.from_rows(told_rows, now_ms=now_ms, storyline_key=storyline_key),
            now_ms=int(now_ms),
            queue_lag_ms=max(0, int(queue_lag_ms)),
        )

    def model_payload(self) -> dict[str, Any]:
        """Return bounded model-visible evidence with audit-only ids removed."""

        event = self.evidence
        return _ModelVisibleTriageInput(
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


class EventSemantics(_ExactModel):
    novelty: Literal["new_fact", "progression", "restatement"]
    restates: int = Field(default=-1, ge=-1)
    event_type: Literal[
        "listing",
        "delisting",
        "filing",
        "regulation",
        "hack",
        "exploit",
        "partnership",
        "funding",
        "macro",
        "rates",
        "oi_spike",
        "liquidation",
        "whale",
        "earnings",
        "product",
        "rumor",
        "noise",
    ]
    assets: tuple[TriageAsset, ...] = Field(default=(), max_length=8)
    direction: Literal["bullish", "bearish", "neutral", "unclear"]
    scope: Literal["macro", "sector", "single_name"]
    magnitude: int = Field(ge=0, le=3)
    actionable: bool
    confidence: float = Field(ge=0.0, le=1.0)
    decision: Literal["push", "drop", "escalate"]
    audience: Literal["crypto", "us_equity", "macro", "none"] = "none"


class ReaderCard(_ExactModel):
    headline_zh: str = Field(min_length=1, max_length=60)
    title_zh: str = Field(default="", max_length=160)
    why_zh: str = Field(default="", max_length=140)

    @model_validator(mode="after")
    def _headline_has_content(self) -> ReaderCard:
        if not self.headline_zh.strip():
            raise ValueError("news_program_reader_headline_empty")
        return self


class ExecutionContract(_ExactModel):
    normal_calls: Literal[2] = 2
    max_calls_per_route: Literal[3] = 3
    max_calls_per_chain: Literal[6] = 6
    shared_fast_retry_per_route: Literal[1] = 1
    fallback_restarts_graph: Literal[True] = True
    cache: Literal[False] = False
    hidden_retries: Literal[False] = False
    route_deadline_seconds: int = Field(ge=1, le=120)
    primary_breaker_failures: int = Field(ge=1, le=100)
    primary_breaker_open_seconds: int = Field(ge=1, le=3600)


class PredictorModelBindings(_ExactModel):
    primary: str
    fallback: str

    @field_validator("primary", "fallback")
    @classmethod
    def _known_slot(cls, value: str) -> str:
        if value not in _MODEL_BINDING_SLOTS:
            raise ValueError("news_program_model_binding_unknown")
        return value


class PredictorState(_ExactModel):
    name: Literal["event_semantics", "reader_card"]
    signature_id: str
    signature_sha256: str
    instruction: str = Field(min_length=1, max_length=PROGRAM_INSTRUCTION_MAX_BYTES)
    instruction_sha256: str
    demos: tuple[dict[str, Any], ...] = Field(default=(), max_length=PROGRAM_DEMOS_MAX)
    demos_sha256: str
    model_bindings: PredictorModelBindings
    max_tokens: int = Field(ge=64, le=4096)

    @model_validator(mode="after")
    def _validate_hashes(self) -> PredictorState:
        if len(self.instruction.encode("utf-8")) > PROGRAM_INSTRUCTION_MAX_BYTES:
            raise ValueError(f"news_program_{self.name}_instruction_too_large")
        if self.instruction_sha256 != canonical_sha(self.instruction):
            raise ValueError(f"news_program_{self.name}_instruction_hash_mismatch")
        if self.demos_sha256 != canonical_sha(list(self.demos)):
            raise ValueError(f"news_program_{self.name}_demos_hash_mismatch")
        _validate_predictor_demos(self.name, self.demos)
        return self


class CompileProvenance(_ExactModel):
    mode: Literal["code_owned_baseline", "optimizer_candidate"]
    development_dataset_sha: str | None = None
    learning_epoch: str = Field(min_length=1)
    optimizer: str = Field(min_length=1)
    dspy_version: Literal["3.3.0"] = "3.3.0"
    gepa_version: Literal["none", "0.1.1"]
    metric_sha256: str | None = None
    optimizer_config_sha256: str | None = None
    seed: int | None = Field(default=None, ge=0)
    max_metric_calls: int = Field(ge=0)
    max_task_model_calls: int = Field(ge=0)
    max_cost_microusd: int = Field(ge=0)
    metric_calls: int = Field(ge=0)
    task_model_calls: int = Field(ge=0)
    reflection_model_calls: int = Field(ge=0)
    actual_cost_microusd: int = Field(ge=0)
    trajectory_sha256: str | None = None
    checkpoint_sha256: str | None = None
    holdout_access_attestation: Literal[False] = False

    @field_validator(
        "development_dataset_sha",
        "metric_sha256",
        "optimizer_config_sha256",
        "trajectory_sha256",
        "checkpoint_sha256",
    )
    @classmethod
    def _optional_sha256(cls, value: str | None) -> str | None:
        if value is not None and (len(value) != 64 or any(char not in "0123456789abcdef" for char in value)):
            raise ValueError("news_program_compile_provenance_sha_invalid")
        return value

    @model_validator(mode="after")
    def _validate_mode(self) -> CompileProvenance:
        optional_values = (
            self.development_dataset_sha,
            self.metric_sha256,
            self.optimizer_config_sha256,
            self.seed,
            self.trajectory_sha256,
            self.checkpoint_sha256,
        )
        if self.mode == "code_owned_baseline":
            if (
                self.learning_epoch != "code_owned/no_epoch"
                or self.optimizer != "code_owned/no_optimizer"
                or self.gepa_version != "none"
                or any(value is not None for value in optional_values)
                or any(
                    value != 0
                    for value in (
                        self.max_metric_calls,
                        self.max_task_model_calls,
                        self.max_cost_microusd,
                        self.metric_calls,
                        self.task_model_calls,
                        self.reflection_model_calls,
                        self.actual_cost_microusd,
                    )
                )
            ):
                raise ValueError("news_program_baseline_compile_provenance_invalid")
            return self
        if (
            any(value is None for value in optional_values)
            or self.optimizer != "dspy.GEPA@3.3.0/gepa@0.1.1"
            or self.gepa_version != "0.1.1"
            or self.max_metric_calls <= 0
            or self.max_task_model_calls <= 0
            or self.max_cost_microusd <= 0
            or self.metric_calls > self.max_metric_calls
            or self.task_model_calls + self.reflection_model_calls > self.max_task_model_calls
            or self.actual_cost_microusd > self.max_cost_microusd
        ):
            raise ValueError("news_program_candidate_compile_provenance_invalid")
        return self


class CompileReceipt(CompileProvenance):
    compiler: str
    source: str
    accepted_by: Literal["code_owner", "unaccepted_candidate"]


class ProgramArtifact(_ExactModel):
    """Immutable, code-owned, canonical JSON state for one DSPy Program."""

    schema_version: Literal["news_semantic_program_artifact_v1"]
    program_version: str
    program_sha256: str
    state_sha256: str
    parent_program_sha256: str | None = None
    factory_id: Literal["tracefold.news.semantic_program.factory_v1"]
    factory_source_sha256: str
    topology_sha256: str
    dspy_version: Literal["3.3.0"]
    dependency_lock_sha256: str
    input_contract_sha256: str
    adapter_sha256: str
    assembler_sha256: str
    execution: ExecutionContract
    event_semantics: PredictorState
    reader_card: PredictorState
    compile_receipt: CompileReceipt

    @model_validator(mode="after")
    def _validate_known_factory(self) -> ProgramArtifact:
        if self.factory_source_sha256 != _runtime_factory_source_sha256():
            raise ValueError("news_program_factory_source_unknown")
        if self.topology_sha256 != PROGRAM_TOPOLOGY_SHA256:
            raise ValueError("news_program_topology_unknown")
        if self.adapter_sha256 != PROGRAM_ADAPTER_SHA256:
            raise ValueError("news_program_adapter_unknown")
        if self.assembler_sha256 != PROGRAM_ASSEMBLER_SHA256:
            raise ValueError("news_program_assembler_unknown")
        if self.input_contract_sha256 != PROGRAM_INPUT_CONTRACT_SHA256:
            raise ValueError("news_program_input_contract_unknown")
        if self.event_semantics.signature_sha256 != EVENT_SEMANTICS_SIGNATURE_SHA256:
            raise ValueError("news_program_event_semantics_signature_unknown")
        if self.reader_card.signature_sha256 != READER_CARD_SIGNATURE_SHA256:
            raise ValueError("news_program_reader_card_signature_unknown")
        if self.event_semantics.signature_id != "tracefold.news.EventSemantics.v1":
            raise ValueError("news_program_event_semantics_signature_id_unknown")
        if self.reader_card.signature_id != "tracefold.news.ReaderCard.v1":
            raise ValueError("news_program_reader_card_signature_id_unknown")
        if self.dependency_lock_sha256 != _runtime_dependency_lock_sha256():
            raise ValueError("news_program_dependency_lock_mismatch")
        if importlib.metadata.version("dspy") != self.dspy_version:
            raise ValueError("news_program_dspy_version_mismatch")
        if self.event_semantics.name != "event_semantics" or self.reader_card.name != "reader_card":
            raise ValueError("news_program_predictor_order_invalid")
        expected_bindings = {
            "event_semantics": {
                "primary": "event_semantics.primary",
                "fallback": "event_semantics.fallback",
            },
            "reader_card": {
                "primary": "reader_card.primary",
                "fallback": "reader_card.fallback",
            },
        }
        for state in (self.event_semantics, self.reader_card):
            if state.model_bindings.model_dump() != expected_bindings[state.name]:
                raise ValueError(f"news_program_{state.name}_model_bindings_invalid")
        if self.parent_program_sha256 is None:
            if self.compile_receipt.mode != "code_owned_baseline" or self.compile_receipt.accepted_by != "code_owner":
                raise ValueError("news_program_baseline_parent_receipt_invalid")
        elif (
            self.compile_receipt.mode != "optimizer_candidate"
            or self.compile_receipt.accepted_by != "unaccepted_candidate"
        ):
            raise ValueError("news_program_candidate_parent_receipt_invalid")
        if self.state_sha256 != self.computed_state_sha256():
            raise ValueError("news_program_state_hash_mismatch")
        return self

    def state(self) -> dict[str, Any]:
        return {
            "event_semantics": self.event_semantics.model_dump(mode="json"),
            "reader_card": self.reader_card.model_dump(mode="json"),
        }

    def manifest(self, *, include_program_sha256: bool = True) -> dict[str, Any]:
        excluded = {"event_semantics", "reader_card"}
        if not include_program_sha256:
            excluded.add("program_sha256")
        return self.model_dump(mode="json", exclude=excluded)

    def computed_state_sha256(self) -> str:
        return canonical_sha(self.state())

    def computed_sha256(self) -> str:
        return canonical_sha(self.manifest(include_program_sha256=False))


class _ProgramManifest(_ExactModel):
    schema_version: Literal["news_semantic_program_artifact_v1"]
    program_version: str
    program_sha256: str
    state_sha256: str
    parent_program_sha256: str | None = None
    factory_id: Literal["tracefold.news.semantic_program.factory_v1"]
    factory_source_sha256: str
    topology_sha256: str
    dspy_version: Literal["3.3.0"]
    dependency_lock_sha256: str
    input_contract_sha256: str
    adapter_sha256: str
    assembler_sha256: str
    execution: ExecutionContract
    compile_receipt: CompileReceipt


class _ProgramState(_ExactModel):
    event_semantics: PredictorState
    reader_card: PredictorState


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"news_program_duplicate_key:{key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"news_program_json_nonfinite:{value}")


def _reject_nonfinite_json(value: Any, *, path: str = "artifact") -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"news_program_json_nonfinite:{path}")
    if isinstance(value, Mapping):
        for key, child in value.items():
            _reject_nonfinite_json(child, path=f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _reject_nonfinite_json(child, path=f"{path}[{index}]")


def _state_key_parts(raw_key: object) -> tuple[str, ...]:
    separated = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(raw_key))
    return tuple(part for part in re.split(r"[^a-z0-9]+", separated.casefold()) if part)


def _unsafe_state_key(raw_key: object) -> bool:
    parts = _state_key_parts(raw_key)
    for forbidden in _FORBIDDEN_STATE_KEY_PARTS:
        width = len(forbidden)
        if any(parts[index : index + width] == forbidden for index in range(len(parts) - width + 1)):
            return True
    return False


def _reject_unsafe_state(value: Any, *, path: str = "artifact") -> None:
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            if _unsafe_state_key(raw_key):
                raise ValueError(f"news_program_unsafe_state_key:{path}.{raw_key}")
            _reject_unsafe_state(child, path=f"{path}.{raw_key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_unsafe_state(child, path=f"{path}[{index}]")


def _validate_predictor_demos(name: str, demos: Sequence[Mapping[str, Any]]) -> None:
    expected = _DEMO_FIELDS.get(name)
    if expected is None:
        raise ValueError("news_program_demo_predictor_unknown")
    for index, demo in enumerate(demos):
        if set(demo) != expected:
            raise ValueError(f"news_program_{name}_demo_fields_invalid:{index}")
        _reject_unsafe_state(demo, path=f"state.{name}.demos[{index}]")
        evidence = _canonical_demo_json(
            demo["evidence_json"],
            predictor=name,
            field="evidence_json",
            index=index,
        )
        _reject_unsafe_state(evidence, path=f"state.{name}.demos[{index}].evidence_json")
        try:
            visible_input = _ModelVisibleTriageInput.model_validate(evidence)
            if canonical_json(visible_input.model_dump(mode="json")) != demo["evidence_json"]:
                raise ValueError("evidence_json_not_typed_canonical")
            if name == "event_semantics":
                EventSemantics.model_validate(demo["semantics"])
            else:
                semantics = _canonical_demo_json(
                    demo["semantics_json"],
                    predictor=name,
                    field="semantics_json",
                    index=index,
                )
                validated_semantics = EventSemantics.model_validate(semantics)
                if canonical_json(validated_semantics.model_dump(mode="json")) != demo["semantics_json"]:
                    raise ValueError("semantics_json_not_typed_canonical")
                ReaderCard.model_validate(demo["card"])
        except (TypeError, ValidationError, ValueError) as exc:
            raise ValueError(f"news_program_{name}_demo_value_invalid:{index}") from exc


def _canonical_demo_json(
    value: Any,
    *,
    predictor: str,
    field: str,
    index: int,
) -> dict[str, Any]:
    if not isinstance(value, str) or len(value.encode("utf-8")) > PROGRAM_DEMO_JSON_MAX_BYTES:
        raise ValueError(f"news_program_{predictor}_demo_{field}_size_invalid:{index}")
    try:
        parsed = json.loads(
            value,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"news_program_{predictor}_demo_{field}_json_invalid:{index}") from exc
    if not isinstance(parsed, dict) or canonical_json(parsed) != value:
        raise ValueError(f"news_program_{predictor}_demo_{field}_json_noncanonical:{index}")
    _reject_nonfinite_json(parsed, path=f"state.{predictor}.demos[{index}].{field}")
    return parsed


class ProgramArtifactCodec:
    """Strict codec for the sole supported ProgramArtifact representation."""

    @classmethod
    def _json_object(cls, document: str | bytes, *, kind: str) -> dict[str, Any]:
        try:
            text = document.decode("utf-8") if isinstance(document, bytes) else document
            raw = json.loads(
                text,
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_json_constant,
            )
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            raise ValueError(f"news_program_{kind}_json_invalid") from exc
        if not isinstance(raw, dict):
            raise ValueError(f"news_program_{kind}_must_be_object")
        canonical_document = canonical_json(raw)
        if text not in {canonical_document, canonical_document + "\n"}:
            raise ValueError(f"news_program_{kind}_json_noncanonical")
        _reject_nonfinite_json(raw)
        _reject_unsafe_state(raw)
        return raw

    @classmethod
    def decode(cls, manifest_document: str | bytes, state_document: str | bytes) -> ProgramArtifact:
        manifest_raw = cls._json_object(manifest_document, kind="manifest")
        state_raw = cls._json_object(state_document, kind="state")
        try:
            manifest = _ProgramManifest.model_validate(manifest_raw)
            state = _ProgramState.model_validate(state_raw)
            artifact = ProgramArtifact.model_validate(
                {**manifest.model_dump(mode="json"), **state.model_dump(mode="json")}
            )
        except ValidationError as exc:
            raise ValueError("news_program_artifact_schema_invalid") from exc
        if manifest.state_sha256 != canonical_sha(state.model_dump(mode="json")):
            raise ValueError("news_program_state_hash_mismatch")
        if artifact.program_sha256 != artifact.computed_sha256():
            raise ValueError("news_program_artifact_hash_mismatch")
        return artifact

    @staticmethod
    def encode(artifact: ProgramArtifact) -> tuple[str, str]:
        _reject_nonfinite_json(artifact.model_dump(mode="json"))
        if artifact.state_sha256 != artifact.computed_state_sha256():
            raise ValueError("news_program_state_hash_mismatch")
        if artifact.program_sha256 != artifact.computed_sha256():
            raise ValueError("news_program_artifact_hash_mismatch")
        return canonical_json(artifact.manifest()) + "\n", canonical_json(artifact.state()) + "\n"

    @classmethod
    def load(cls, path: str | None = None) -> ProgramArtifact:
        if path is None:
            return load_stable_program_artifact()
        requested = Path(path)
        if ".." in requested.parts:
            raise ValueError("news_program_artifact_path_invalid")
        try:
            candidate = requested.resolve(strict=True)
        except OSError as exc:
            raise ValueError("news_program_artifact_path_invalid") from exc
        if requested.absolute() != candidate or requested.is_symlink() or not candidate.is_dir():
            raise ValueError("news_program_artifact_path_invalid")
        children = {child.name for child in candidate.iterdir()}
        if children != {"manifest.json", "state.json"}:
            raise ValueError("news_program_artifact_files_invalid")
        manifest_path = candidate / "manifest.json"
        state_path = candidate / "state.json"
        if (
            manifest_path.is_symlink()
            or state_path.is_symlink()
            or not manifest_path.is_file()
            or not state_path.is_file()
        ):
            raise ValueError("news_program_artifact_files_invalid")
        artifact = cls.decode(
            manifest_path.read_text(encoding="utf-8"),
            state_path.read_text(encoding="utf-8"),
        )
        if candidate.name != artifact.program_sha256:
            raise ValueError("news_program_artifact_directory_identity_mismatch")
        return artifact

    @classmethod
    def from_compiled_module(
        cls,
        module: DspyCompileProgram,
        *,
        base_artifact: ProgramArtifact,
        compiler: str,
        source: str,
        compile_provenance: CompileProvenance,
    ) -> ProgramArtifact:
        """Freeze only safe Predictor instructions/demos from a cold compiled Module."""

        if not isinstance(module, DspyCompileProgram):
            raise TypeError("news_program_compiled_module_type_invalid")
        event_state = _compiled_predictor_state(base_artifact.event_semantics, module.event_semantics)
        reader_state = _compiled_predictor_state(base_artifact.reader_card, module.reader_card)
        data = base_artifact.model_dump(mode="json")
        data.update(
            {
                "event_semantics": event_state.model_dump(mode="json"),
                "reader_card": reader_state.model_dump(mode="json"),
                "parent_program_sha256": base_artifact.program_sha256,
                "compile_receipt": {
                    **compile_provenance.model_dump(mode="json"),
                    "compiler": str(compiler),
                    "source": str(source),
                    "accepted_by": "unaccepted_candidate",
                },
            }
        )
        data["state_sha256"] = canonical_sha(
            {"event_semantics": data["event_semantics"], "reader_card": data["reader_card"]}
        )
        manifest_without_program = {
            key: value for key, value in data.items() if key not in {"program_sha256", "event_semantics", "reader_card"}
        }
        data["program_sha256"] = canonical_sha(manifest_without_program)
        return ProgramArtifact.model_validate(data)


def load_stable_program_artifact() -> ProgramArtifact:
    """Load and re-verify the immutable code-owned stable ProgramArtifact."""

    registry = _load_program_registry()
    return load_program_artifact(str(registry["stable"]))


def _programs_resource_root() -> Any:
    root = importlib.resources.files("tracefold.news.agents").joinpath("programs")
    if not isinstance(root, Path):
        # Zip/importlib Traversables have no filesystem symlink surface.  Their
        # bytes still pass the same strict registry and artifact codec below.
        return root
    try:
        resolved = root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError("news_program_registry_path_invalid") from exc
    if root.is_symlink() or root.absolute() != resolved or not resolved.is_dir():
        raise ValueError("news_program_registry_path_invalid")
    return resolved


def _verified_resource_child(root: Any, name: str, *, kind: Literal["file", "directory"]) -> Any:
    child = root.joinpath(name)
    if not isinstance(root, Path) or not isinstance(child, Path):
        return child
    try:
        resolved_root = root.resolve(strict=True)
        resolved_child = child.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError("news_program_artifact_path_invalid") from exc
    valid_kind = resolved_child.is_file() if kind == "file" else resolved_child.is_dir()
    if (
        child.is_symlink()
        or child.absolute() != resolved_child
        or resolved_child.parent != resolved_root
        or not valid_kind
    ):
        raise ValueError("news_program_artifact_path_invalid")
    return resolved_child


def _load_program_registry() -> dict[str, Any]:
    root = _programs_resource_root()
    registry_resource = _verified_resource_child(root, "registry.json", kind="file")
    if not registry_resource.is_file():
        raise ValueError("news_program_registry_path_invalid")
    raw = ProgramArtifactCodec._json_object(registry_resource.read_text(encoding="utf-8"), kind="registry")
    if set(raw) != {"stable", "images"} or not isinstance(raw["images"], list):
        raise ValueError("news_program_registry_schema_invalid")
    images = [str(value) for value in raw["images"]]
    if str(raw["stable"]) not in images or len(images) != len(set(images)):
        raise ValueError("news_program_registry_identity_invalid")
    if any(len(value) != 64 or any(char not in "0123456789abcdef" for char in value) for value in images):
        raise ValueError("news_program_registry_sha_invalid")
    return {"stable": str(raw["stable"]), "images": tuple(images)}


def load_program_artifact(program_sha256: str) -> ProgramArtifact:
    """Resolve one immutable image from the code-owned registry, never from a user path."""

    identity = str(program_sha256)
    registry = _load_program_registry()
    if identity not in registry["images"]:
        raise ValueError("news_program_artifact_not_registered")
    root = _programs_resource_root()
    image = _verified_resource_child(root, identity, kind="directory")
    if not image.is_dir():
        raise ValueError("news_program_artifact_path_invalid")
    children = {child.name for child in image.iterdir()}
    if children != {"manifest.json", "state.json"}:
        raise ValueError("news_program_artifact_files_invalid")
    manifest_resource = image.joinpath("manifest.json")
    state_resource = image.joinpath("state.json")
    if isinstance(image, Path):
        manifest_resource = _verified_resource_child(image, "manifest.json", kind="file")
        state_resource = _verified_resource_child(image, "state.json", kind="file")
    if not manifest_resource.is_file() or not state_resource.is_file():
        raise ValueError("news_program_artifact_files_invalid")
    if (
        isinstance(manifest_resource, Path)
        and isinstance(state_resource, Path)
        and (manifest_resource.is_symlink() or state_resource.is_symlink())
    ):
        raise ValueError("news_program_artifact_files_invalid")
    artifact = ProgramArtifactCodec.decode(
        manifest_resource.read_text(encoding="utf-8"),
        state_resource.read_text(encoding="utf-8"),
    )
    if artifact.program_sha256 != identity:
        raise ValueError("news_program_artifact_directory_identity_mismatch")
    return artifact


class PredictorRequest(_ExactModel):
    program_version: str
    program_sha256: str
    context_sha256: str
    predictor: Literal["event_semantics", "reader_card"]
    route: Literal["primary", "fallback"]
    attempt: int = Field(ge=1, le=2)
    signature_sha256: str
    instruction_sha256: str
    demos_sha256: str
    adapter_sha256: str
    model_binding: str
    runtime_provider: str = Field(min_length=1)
    runtime_model: str = Field(min_length=1)
    runtime_model_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    runtime_binding_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    upstream_sha256: str | None = None
    inputs: dict[str, Any]

    @model_validator(mode="after")
    def _runtime_identity_matches(self) -> PredictorRequest:
        RuntimeModelIdentity(
            provider=self.runtime_provider,
            model=self.runtime_model,
            model_sha256=self.runtime_model_sha256,
            binding_sha256=self.runtime_binding_sha256,
        )
        return self

    @property
    def request_sha256(self) -> str:
        return canonical_sha(self.model_dump(mode="json"))


class PredictorResponse(_ExactModel):
    output: dict[str, Any]
    provider: str | None = None
    model: str | None = None
    model_sha256: str | None = None
    latency_ms: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cached_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    provider_cost_microusd: int | None = Field(default=None, ge=0)
    finish_reason: str | None = None
    runtime_binding_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @property
    def provider_cost_usd(self) -> float | None:
        return None if self.provider_cost_microusd is None else self.provider_cost_microusd / 1_000_000


class ProviderCallObservation(_ExactModel):
    """Safe metadata from one provider response whose output could not parse."""

    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    model_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    latency_ms: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cached_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    provider_cost_microusd: int | None = Field(default=None, ge=0)
    finish_reason: str | None = None
    runtime_binding_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _model_identity_matches(self) -> ProviderCallObservation:
        expected = canonical_sha({"provider": self.provider, "model": self.model})
        if self.model_sha256 != expected:
            raise ValueError("news_program_provider_observation_model_identity_mismatch")
        return self


class PredictorAdapterError(Exception):
    def __init__(
        self,
        code: str,
        *,
        retryable: bool = False,
        output_failure: bool = False,
        finish_reason: str | None = None,
        provider_observation: ProviderCallObservation | None = None,
        partial_output: Mapping[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.retryable = retryable
        self.output_failure = output_failure
        self.finish_reason = finish_reason
        self.provider_observation = provider_observation
        self.partial_output = dict(partial_output) if partial_output is not None else None
        super().__init__(code)


@runtime_checkable
class PredictorAdapter(Protocol):
    def runtime_identity(self, model_binding: str) -> RuntimeModelIdentity: ...

    async def invoke(self, request: PredictorRequest, predictor: dspy.Predict) -> PredictorResponse: ...


class DspyStrictJSONAdapter(dspy.JSONAdapter):  # type: ignore[misc]
    """DSPy JSON Adapter with one format and no implicit format fallback."""

    def __call__(
        self,
        lm: dspy.BaseLM,
        lm_kwargs: dict[str, Any],
        signature: type[dspy.Signature],
        demos: list[dict[str, Any]],
        inputs: dict[str, Any],
    ) -> list[dict[str, Any]]:
        def call_chat(
            inner_lm: dspy.BaseLM,
            inner_kwargs: dict[str, Any],
            inner_signature: type[dspy.Signature],
            inner_demos: list[dict[str, Any]],
            inner_inputs: dict[str, Any],
        ) -> list[dict[str, Any]]:
            return cast(
                list[dict[str, Any]],
                ChatAdapter.__call__(
                    self,
                    inner_lm,
                    inner_kwargs,
                    inner_signature,
                    inner_demos,
                    inner_inputs,
                ),
            )

        result = self._json_adapter_call_common(lm, lm_kwargs, signature, demos, inputs, call_chat)
        if result is not None:
            return cast(list[dict[str, Any]], result)
        lm_kwargs["response_format"] = _get_structured_outputs_response_format(
            signature, self.use_native_function_calling
        )
        return call_chat(lm, lm_kwargs, signature, demos, inputs)

    async def acall(
        self,
        lm: dspy.BaseLM,
        lm_kwargs: dict[str, Any],
        signature: type[dspy.Signature],
        demos: list[dict[str, Any]],
        inputs: dict[str, Any],
    ) -> list[dict[str, Any]]:
        async def call_chat(
            inner_lm: dspy.BaseLM,
            inner_kwargs: dict[str, Any],
            inner_signature: type[dspy.Signature],
            inner_demos: list[dict[str, Any]],
            inner_inputs: dict[str, Any],
        ) -> list[dict[str, Any]]:
            return cast(
                list[dict[str, Any]],
                await ChatAdapter.acall(
                    self,
                    inner_lm,
                    inner_kwargs,
                    inner_signature,
                    inner_demos,
                    inner_inputs,
                ),
            )

        result = self._json_adapter_call_common(lm, lm_kwargs, signature, demos, inputs, call_chat)
        if result is not None:
            return cast(list[dict[str, Any]], await result)
        lm_kwargs["response_format"] = _get_structured_outputs_response_format(
            signature, self.use_native_function_calling
        )
        return await call_chat(lm, lm_kwargs, signature, demos, inputs)


class DspyPredictorAdapter:
    """Production Adapter for one explicitly configured DSPy LM.

    The constructor rejects DSPy's default cache and retry settings.  This is
    important: the ProgramTrace call count must equal provider attempts.
    """

    def __init__(
        self,
        lm: dspy.LM,
        *,
        model_name: str,
        model_sha256: str | None = None,
        provider: str | None = None,
        adapter: dspy.Adapter | None = None,
    ) -> None:
        if getattr(lm, "cache", True) is not False:
            raise ValueError("news_program_lm_cache_must_be_disabled")
        if int(getattr(lm, "num_retries", -1)) != 0:
            raise ValueError("news_program_lm_hidden_retries_must_be_zero")
        if not callable(getattr(lm, "observe_exact_call", None)):
            raise ValueError("news_program_lm_exact_metadata_seam_required")
        self._lm = lm
        self._model_name = str(model_name)
        self._provider = provider or (
            self._model_name.split("/", maxsplit=1)[0] if "/" in self._model_name else "unknown"
        )
        self._runtime = RuntimeModelIdentity.issue(
            provider=self._provider,
            model=self._model_name,
            model_sha256=model_sha256,
        )
        self._adapter = adapter or DspyStrictJSONAdapter(use_native_function_calling=False)

    def runtime_identity(self, model_binding: str) -> RuntimeModelIdentity:
        del model_binding
        return self._runtime

    @classmethod
    def from_runtime(
        cls,
        *,
        model_name: str,
        api_key: str,
        api_base: str,
        timeout: float,
        max_tokens: int,
        model_sha256: str | None = None,
        provider: str | None = None,
        model_kwargs: Mapping[str, Any] | None = None,
    ) -> DspyPredictorAdapter:
        """Compose the only supported production LM without leaking DSPy outside this Module."""

        extras = dict(model_kwargs or {})
        owned = {"api_key", "api_base", "base_url", "cache", "num_retries", "temperature", "max_tokens", "timeout"}
        overlap = owned.intersection(extras)
        if overlap:
            raise ValueError(f"news_program_runtime_model_kwargs_owned:{','.join(sorted(overlap))}")
        lm = ExactMetadataDspyLM(
            str(model_name),
            api_key=str(api_key),
            api_base=str(api_base),
            temperature=0,
            max_tokens=int(max_tokens),
            timeout=float(timeout),
            cache=False,
            num_retries=0,
            **extras,
        )
        return cls(lm, model_name=str(model_name), model_sha256=model_sha256, provider=provider)

    async def invoke(self, request: PredictorRequest, predictor: dspy.Predict) -> PredictorResponse:
        if request.runtime_binding_sha256 != self._runtime.binding_sha256:
            raise PredictorAdapterError("news_program_runtime_binding_mismatch")
        started = time.perf_counter()
        capture: ExactProviderCallCapture
        try:
            with (
                self._lm.observe_exact_call() as capture,
                dspy.context(lm=self._lm, adapter=self._adapter, track_usage=True, disable_history=True),
            ):
                prediction = await predictor.acall(**request.inputs)
        except (AdapterParseError, ValidationError) as exc:
            metadata = capture.require_exactly_one()
            finish_reason = metadata.finish_reason
            elapsed = max(0, round((time.perf_counter() - started) * 1000))
            response_model = metadata.response_model or self._model_name
            observation = ProviderCallObservation(
                provider=self._provider,
                model=response_model,
                model_sha256=canonical_sha({"provider": self._provider, "model": response_model}),
                latency_ms=elapsed,
                input_tokens=metadata.input_tokens,
                output_tokens=metadata.output_tokens,
                cached_tokens=metadata.cached_tokens,
                total_tokens=metadata.total_tokens,
                provider_cost_microusd=metadata.provider_cost_microusd,
                finish_reason=finish_reason,
                runtime_binding_sha256=self._runtime.binding_sha256,
            )
            code = (
                "news_program_output_truncated"
                if finish_reason in _TRUNCATED_FINISH_REASONS
                else f"news_program_dspy_output_{type(exc).__name__.casefold()}"
            )
            raise PredictorAdapterError(
                code,
                output_failure=True,
                finish_reason=finish_reason,
                provider_observation=observation,
                partial_output=_safe_adapter_partial_output(exc, predictor=request.predictor),
            ) from exc
        metadata = capture.require_exactly_one()
        elapsed = max(0, round((time.perf_counter() - started) * 1000))
        output = prediction.toDict()
        response_model = metadata.response_model or self._model_name
        return PredictorResponse(
            output=output,
            provider=self._provider,
            model=response_model,
            model_sha256=canonical_sha({"provider": self._provider, "model": response_model}),
            latency_ms=elapsed,
            input_tokens=metadata.input_tokens,
            output_tokens=metadata.output_tokens,
            cached_tokens=metadata.cached_tokens,
            total_tokens=metadata.total_tokens,
            provider_cost_microusd=metadata.provider_cost_microusd,
            finish_reason=metadata.finish_reason,
            runtime_binding_sha256=self._runtime.binding_sha256,
        )


ScriptedStep = PredictorResponse | Mapping[str, Any] | BaseException | Callable[[PredictorRequest], Any]


class ScriptedPredictorAdapter:
    """Deterministic ordered Adapter Seam for unit tests and offline probes."""

    def __init__(
        self,
        steps: Sequence[ScriptedStep],
        *,
        model_name: str = "scripted/test",
        provider: str = "scripted",
    ) -> None:
        self._steps = list(steps)
        self.model_name = model_name
        self.provider = provider
        self._runtime = RuntimeModelIdentity.issue(provider=provider, model=model_name)
        self.requests: list[PredictorRequest] = []

    def runtime_identity(self, model_binding: str) -> RuntimeModelIdentity:
        del model_binding
        return self._runtime

    async def invoke(self, request: PredictorRequest, predictor: dspy.Predict) -> PredictorResponse:
        del predictor
        self.requests.append(request)
        if not self._steps:
            raise PredictorAdapterError("news_program_script_exhausted")
        step = self._steps.pop(0)
        if callable(step):
            step = step(request)
        if isinstance(step, BaseException):
            raise step
        if isinstance(step, PredictorResponse):
            if step.runtime_binding_sha256 not in {None, request.runtime_binding_sha256}:
                raise PredictorAdapterError("news_program_runtime_binding_mismatch")
            return step.model_copy(
                update={
                    "provider": step.provider or self.provider,
                    "model": step.model or self.model_name,
                    "model_sha256": step.model_sha256
                    or canonical_sha({"provider": self.provider, "model": step.model or self.model_name}),
                    "runtime_binding_sha256": request.runtime_binding_sha256,
                }
            )
        return PredictorResponse(
            output=dict(step),
            provider=self.provider,
            model=self.model_name,
            model_sha256=self._runtime.model_sha256,
            runtime_binding_sha256=request.runtime_binding_sha256,
        )


class PredictorRecording(_ExactModel):
    request: dict[str, Any]
    response: PredictorResponse

    @model_validator(mode="after")
    def _identity_is_exact(self) -> PredictorRecording:
        runtime_binding_sha = str(self.request.get("runtime_binding_sha256") or "")
        if len(runtime_binding_sha) != 64:
            raise ValueError("news_program_recording_model_identity_missing")
        if self.response.runtime_binding_sha256 != runtime_binding_sha:
            raise ValueError("news_program_recording_model_identity_mismatch")
        return self


class RecordReplayPredictorAdapter:
    """Strict request-addressed replay; a miss can never fall through to live I/O."""

    def __init__(self, recordings: Mapping[str, PredictorRecording | Mapping[str, Any]]) -> None:
        parsed: dict[str, PredictorRecording] = {}
        identities: dict[str, RuntimeModelIdentity] = {}
        for raw_sha, raw_recording in recordings.items():
            request_sha = str(raw_sha)
            recording = (
                raw_recording
                if isinstance(raw_recording, PredictorRecording)
                else PredictorRecording.model_validate(raw_recording)
            )
            recorded_sha = str(recording.request.get("request_sha256") or "")
            if request_sha != recorded_sha:
                raise ValueError("news_program_recording_request_identity_mismatch")
            identity = RuntimeModelIdentity(
                provider=str(recording.request.get("runtime_provider") or ""),
                model=str(recording.request.get("runtime_model") or ""),
                model_sha256=str(recording.request.get("runtime_model_sha256") or ""),
                binding_sha256=str(recording.request.get("runtime_binding_sha256") or ""),
            )
            model_binding = str(recording.request.get("model_binding") or "")
            if not model_binding:
                raise ValueError("news_program_recording_model_binding_missing")
            previous = identities.setdefault(model_binding, identity)
            if previous != identity:
                raise ValueError("news_program_recording_model_binding_ambiguous")
            parsed[request_sha] = recording
        self._recordings = parsed
        self._identities = identities
        self.requests: list[PredictorRequest] = []

    def runtime_identity(self, model_binding: str) -> RuntimeModelIdentity:
        identity = self._identities.get(model_binding)
        if identity is None:
            raise PredictorAdapterError("news_program_recording_missing")
        return identity

    async def invoke(self, request: PredictorRequest, predictor: dspy.Predict) -> PredictorResponse:
        del predictor
        self.requests.append(request)
        recording = self._recordings.get(request.request_sha256)
        if recording is None:
            raise PredictorAdapterError("news_program_recording_missing")
        expected = {
            "program_version": request.program_version,
            "program_sha256": request.program_sha256,
            "context_sha256": request.context_sha256,
            "predictor": request.predictor,
            "attempt": request.attempt,
            "route": request.route,
            "request_sha256": request.request_sha256,
            "signature_sha256": request.signature_sha256,
            "instruction_sha256": request.instruction_sha256,
            "demos_sha256": request.demos_sha256,
            "model_binding": request.model_binding,
            "runtime_provider": request.runtime_provider,
            "runtime_model": request.runtime_model,
            "runtime_model_sha256": request.runtime_model_sha256,
            "runtime_binding_sha256": request.runtime_binding_sha256,
            "upstream_sha256": request.upstream_sha256,
        }
        if any(recording.request.get(key) != value for key, value in expected.items()):
            raise PredictorAdapterError("news_program_recording_request_identity_mismatch")
        if recording.response.runtime_binding_sha256 != request.runtime_binding_sha256:
            raise PredictorAdapterError("news_program_recording_model_identity_mismatch")
        return recording.response


class ProgramCallTrace(_ExactModel):
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


class ProgramTrace(_ExactModel):
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


class ProgramUsage(_ExactModel):
    """Aggregates trace entries and distinguishes real provider attempts."""

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


def _aggregate_program_usage(calls: Sequence[ProgramCallTrace]) -> dict[str, Any]:
    physical_calls = [call for call in calls if call.physical_provider_call]
    complete_cost = bool(physical_calls) and all(call.provider_cost_microusd is not None for call in physical_calls)
    return {
        "call_count": len(calls),
        "physical_call_count": len(physical_calls),
        "input_tokens": sum(call.input_tokens for call in calls),
        "output_tokens": sum(call.output_tokens for call in calls),
        "cached_tokens": sum(call.cached_tokens for call in calls),
        "total_tokens": sum(call.total_tokens for call in calls),
        "provider_cost_microusd": (
            sum(cast(int, call.provider_cost_microusd) for call in physical_calls) if complete_cost else None
        ),
    }


class SemanticJudgment(_ExactModel):
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
        expected_usage = _aggregate_program_usage(self.trace.calls)
        actual_usage = self.usage.model_dump(mode="json", exclude={"wall_latency_ms"})
        if actual_usage != expected_usage:
            raise ValueError("news_program_judgment_usage_mismatch")
        return self


class SemanticProgramError(Exception):
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


class _EventSemanticsSignature(dspy.Signature):  # type: ignore[misc]
    evidence_json: str = dspy.InputField(desc="Canonical bounded News evidence JSON")
    semantics: EventSemantics = dspy.OutputField(desc="Strict semantic judgment; no reader prose")


class _ReaderCardSignature(dspy.Signature):  # type: ignore[misc]
    evidence_json: str = dspy.InputField(desc="Canonical bounded News evidence JSON")
    semantics_json: str = dspy.InputField(desc="Validated EventSemantics canonical JSON")
    card: ReaderCard = dspy.OutputField(desc="Concise Chinese reader card")


def _predictor(state: PredictorState, base_signature: type[dspy.Signature]) -> dspy.Predict:
    signature = base_signature.with_instructions(state.instruction)
    predictor = dspy.Predict(signature, temperature=0, max_tokens=state.max_tokens)
    input_names = tuple(
        name for name, field in signature.fields.items() if field.json_schema_extra["__dspy_field_type"] == "input"
    )
    predictor.demos = [dspy.Example(**demo).with_inputs(*input_names) for demo in state.demos]
    return predictor


def _safe_json_state(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _safe_json_state(value.model_dump(mode="json"))
    if isinstance(value, Mapping):
        return {str(key): _safe_json_state(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_json_state(child) for child in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"news_program_compiled_state_type_invalid:{type(value).__name__}")


def _safe_adapter_partial_output(
    exc: AdapterParseError | ValidationError,
    *,
    predictor: Literal["event_semantics", "reader_card"],
) -> dict[str, Any] | None:
    if not isinstance(exc, AdapterParseError):
        return None
    parsed = getattr(exc, "parsed_result", None)
    output_field = "semantics" if predictor == "event_semantics" else "card"
    if not isinstance(parsed, Mapping) or set(parsed) != {output_field}:
        return None
    try:
        safe = _safe_json_state(parsed)
        _reject_unsafe_state(safe, path="provider_partial_output")
    except (TypeError, ValueError):
        return None
    return cast(dict[str, Any], safe)


def _compiled_predictor_state(base: PredictorState, predictor: dspy.Predict) -> PredictorState:
    instructions = str(predictor.signature.instructions)
    demos = tuple(_safe_json_state(demo.toDict()) for demo in predictor.demos)
    return PredictorState.model_validate(
        {
            **base.model_dump(mode="json"),
            "instruction": instructions,
            "instruction_sha256": canonical_sha(instructions),
            "demos": demos,
            "demos_sha256": canonical_sha(list(demos)),
        }
    )


def _is_retryable_exception(exc: BaseException) -> bool:
    if isinstance(
        exc,
        (
            TimeoutError,
            ConnectionError,
            dspy.LMTransportError,
            dspy.LMServerError,
            dspy.LMTimeoutError,
            dspy.LMRateLimitError,
        ),
    ):
        return True
    if isinstance(
        exc,
        (
            dspy.LMAuthError,
            dspy.LMInvalidRequestError,
            dspy.ContextWindowExceededError,
        ),
    ):
        return False
    name = type(exc).__name__.casefold()
    return any(marker in name for marker in _RETRYABLE_MARKERS)


def _unwrap_output(output: Mapping[str, Any], field: str) -> Any:
    if field in output:
        if set(output) != {field}:
            raise ValueError("news_program_output_envelope_extra")
        return output[field]
    return dict(output)


def _assemble(semantics: EventSemantics, card: ReaderCard, *, told_count: int) -> TriageVerdict:
    if semantics.novelty == "restatement":
        if semantics.restates < 0 or semantics.restates >= told_count:
            raise ValueError("news_program_restatement_index_invalid")
    elif semantics.restates != -1:
        raise ValueError("news_program_non_restatement_index_invalid")
    title_zh = "" if card.title_zh.strip() == card.headline_zh.strip() else card.title_zh.strip()
    return TriageVerdict.model_validate(
        {
            **semantics.model_dump(mode="json"),
            "assets": [asset.model_dump(mode="json") for asset in semantics.assets],
            "headline_zh": card.headline_zh.strip(),
            "title_zh": title_zh,
            "why_zh": card.why_zh.strip(),
        }
    )


def _validate_semantic_context(semantics: EventSemantics, *, told_count: int) -> None:
    if semantics.novelty == "restatement":
        if semantics.restates < 0 or semantics.restates >= told_count:
            raise ValueError("news_program_restatement_index_invalid")
    elif semantics.restates != -1:
        raise ValueError("news_program_non_restatement_index_invalid")


class _CallFailure(Exception):
    def __init__(
        self,
        *,
        code: str,
        retryable: bool,
        output_failure: bool,
        finish_reason: str | None,
        trace: ProgramCallTrace,
        raw_output: Mapping[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.retryable = retryable
        self.output_failure = output_failure
        self.finish_reason = finish_reason
        self.trace = trace
        self.raw_output = raw_output
        super().__init__(code)

    @property
    def fast_retryable(self) -> bool:
        return (self.retryable or self.output_failure) and (
            self.finish_reason or ""
        ).casefold() not in _TRUNCATED_FINISH_REASONS


T = TypeVar("T", bound=BaseModel)
AdapterPool = PredictorAdapter | Mapping[str, PredictorAdapter]


class DspyCompileProgram(dspy.Module):  # type: ignore[misc]
    """Cold-only optimizer Module; never used by the production hot path."""

    def __init__(self, artifact: ProgramArtifact) -> None:
        super().__init__()
        if artifact.program_sha256 != artifact.computed_sha256():
            raise ValueError("news_program_artifact_hash_mismatch")
        self.artifact = artifact
        self.event_semantics = _predictor(artifact.event_semantics, _EventSemanticsSignature)
        self.reader_card = _predictor(artifact.reader_card, _ReaderCardSignature)

    def forward(self, evidence_json: str, told_count: int) -> dspy.Prediction:
        semantics_prediction = self.event_semantics(evidence_json=evidence_json)
        semantics = EventSemantics.model_validate(_unwrap_output(semantics_prediction.toDict(), "semantics"))
        card_prediction = self.reader_card(
            evidence_json=evidence_json,
            semantics_json=canonical_json(semantics.model_dump(mode="json")),
        )
        card = ReaderCard.model_validate(_unwrap_output(card_prediction.toDict(), "card"))
        verdict = _assemble(semantics, card, told_count=max(0, int(told_count)))
        return dspy.Prediction(semantics=semantics, card=card, verdict=verdict)


class DspyNewsSemanticProgram(dspy.Module):  # type: ignore[misc]
    """Deep Module owning graph execution, validation, budgets and audit."""

    def __init__(
        self,
        artifact: ProgramArtifact,
        *,
        primary_adapter: AdapterPool,
        fallback_adapter: AdapterPool | None = None,
    ) -> None:
        super().__init__()
        if artifact.program_sha256 != artifact.computed_sha256():
            raise ValueError("news_program_artifact_hash_mismatch")
        self.artifact = artifact
        self.primary_adapter = self._prepare_adapter_pool(primary_adapter, route="primary")
        self.fallback_adapter = (
            self._prepare_adapter_pool(fallback_adapter, route="fallback") if fallback_adapter is not None else None
        )
        self.event_semantics = _predictor(artifact.event_semantics, _EventSemanticsSignature)
        self.reader_card = _predictor(artifact.reader_card, _ReaderCardSignature)
        self._primary_failures = 0
        self._primary_open_until = 0.0

    def _prepare_adapter_pool(
        self,
        pool: AdapterPool,
        *,
        route: Literal["primary", "fallback"],
    ) -> AdapterPool:
        if not isinstance(pool, Mapping):
            return pool
        frozen = dict(pool)
        expected = {
            getattr(self.artifact.event_semantics.model_bindings, route),
            getattr(self.artifact.reader_card.model_bindings, route),
        }
        if set(frozen) != expected:
            raise ValueError("news_program_adapter_bindings_invalid")
        if not all(isinstance(adapter, PredictorAdapter) for adapter in frozen.values()):
            raise TypeError("news_program_adapter_protocol_invalid")
        return frozen

    async def judge(self, context: TriageContext) -> SemanticJudgment:
        if not isinstance(context, TriageContext):
            context = TriageContext.model_validate(context)
        started = time.perf_counter()
        context_sha = canonical_sha(context.model_dump(mode="json"))
        evidence_json = canonical_json(context.model_payload())
        calls: list[ProgramCallTrace] = []
        primary_failure: _CallFailure | None = None
        if time.monotonic() < self._primary_open_until:
            primary_failure = self._circuit_open_failure(context_sha)
        else:
            try:
                result = await self._run_route_with_deadline(
                    route="primary",
                    adapter_pool=self.primary_adapter,
                    context=context,
                    context_sha=context_sha,
                    evidence_json=evidence_json,
                    calls=calls,
                )
            except _CallFailure as exc:
                primary_failure = exc
                if exc.retryable and not exc.output_failure:
                    self._record_primary_failure()
            else:
                self._primary_failures = 0
                self._primary_open_until = 0.0
        if primary_failure is not None:
            if self.fallback_adapter is None:
                raise self._public_error(primary_failure, calls, context_sha=context_sha)
            try:
                result = await self._run_route_with_deadline(
                    route="fallback",
                    adapter_pool=self.fallback_adapter,
                    context=context,
                    context_sha=context_sha,
                    evidence_json=evidence_json,
                    calls=calls,
                )
            except _CallFailure as fallback_exc:
                raise self._public_error(
                    fallback_exc,
                    calls,
                    context_sha=context_sha,
                    primary_failure=primary_failure,
                ) from fallback_exc
        semantics, card, verdict, route, answering_model, novelty_defaulted = result
        semantics_sha = canonical_sha(semantics.model_dump(mode="json"))
        card_sha = canonical_sha(card.model_dump(mode="json"))
        verdict_sha = canonical_sha(verdict.model_dump(mode="json"))
        fallback_from = primary_failure.code if primary_failure is not None else None
        trace = self._trace(
            calls,
            context_sha=context_sha,
            event_semantics_sha256=semantics_sha,
            reader_card_sha256=card_sha,
            verdict_sha256=verdict_sha,
            answering_route=route,
            fallback_from=fallback_from,
            novelty_defaulted=novelty_defaulted,
        )
        usage = self._usage(calls, started=started)
        return SemanticJudgment(
            verdict=verdict,
            program_version=self.artifact.program_version,
            program_sha256=self.artifact.program_sha256,
            trace=trace,
            usage=usage,
            answering_model=answering_model,
            fallback_from=fallback_from,
        )

    async def aforward(self, context: TriageContext) -> SemanticJudgment:
        return await self.judge(context)

    def _record_primary_failure(self) -> None:
        self._primary_failures += 1
        if self._primary_failures >= self.artifact.execution.primary_breaker_failures:
            self._primary_failures = 0
            self._primary_open_until = time.monotonic() + self.artifact.execution.primary_breaker_open_seconds

    def _circuit_open_failure(self, context_sha: str) -> _CallFailure:
        trace = ProgramCallTrace(
            predictor="event_semantics",
            route="primary",
            attempt=1,
            request_sha256=canonical_sha(
                {
                    "program_sha256": self.artifact.program_sha256,
                    "context_sha256": context_sha,
                    "route": "primary",
                    "state": "circuit_open",
                }
            ),
            input_sha256=canonical_sha({"context_sha256": context_sha}),
            signature_sha256=self.artifact.event_semantics.signature_sha256,
            instruction_sha256=self.artifact.event_semantics.instruction_sha256,
            demos_sha256=self.artifact.event_semantics.demos_sha256,
            model_binding=self.artifact.event_semantics.model_bindings.primary,
            error_code="primary_circuit_open",
        )
        return _CallFailure(
            code="primary_circuit_open",
            retryable=False,
            output_failure=False,
            finish_reason=None,
            trace=trace,
        )

    async def _run_route_with_deadline(
        self,
        *,
        route: Literal["primary", "fallback"],
        adapter_pool: AdapterPool,
        context: TriageContext,
        context_sha: str,
        evidence_json: str,
        calls: list[ProgramCallTrace],
    ) -> tuple[EventSemantics, ReaderCard, TriageVerdict, Literal["primary", "fallback"], str | None, bool]:
        route_call_start = len(calls)
        try:
            async with asyncio.timeout(self.artifact.execution.route_deadline_seconds):
                return await self._run_route(
                    route=route,
                    adapter_pool=adapter_pool,
                    context=context,
                    context_sha=context_sha,
                    evidence_json=evidence_json,
                    calls=calls,
                )
        except TimeoutError as exc:
            if len(calls) > route_call_start and calls[-1].error_code == "news_program_route_deadline":
                trace = calls[-1]
            else:
                state = self.artifact.event_semantics
                trace = ProgramCallTrace(
                    predictor="event_semantics",
                    route=route,
                    attempt=1,
                    request_sha256=canonical_sha(
                        {"program_sha256": self.artifact.program_sha256, "context_sha256": context_sha, "route": route}
                    ),
                    input_sha256=canonical_sha({"evidence_json": evidence_json}),
                    signature_sha256=state.signature_sha256,
                    instruction_sha256=state.instruction_sha256,
                    demos_sha256=state.demos_sha256,
                    model_binding=getattr(state.model_bindings, route),
                    error_code="news_program_route_deadline",
                )
                calls.append(trace)
            raise _CallFailure(
                code="news_program_route_deadline",
                retryable=True,
                output_failure=False,
                finish_reason=None,
                trace=trace,
            ) from exc

    @staticmethod
    def _resolve_adapter(
        pool: AdapterPool,
        state: PredictorState,
        route: Literal["primary", "fallback"],
    ) -> PredictorAdapter:
        if not isinstance(pool, Mapping):
            return pool
        binding = getattr(state.model_bindings, route)
        adapter = pool.get(binding)
        if adapter is None:
            raise PredictorAdapterError("news_program_model_binding_unresolved")
        return adapter

    async def _run_route(
        self,
        *,
        route: Literal["primary", "fallback"],
        adapter_pool: AdapterPool,
        context: TriageContext,
        context_sha: str,
        evidence_json: str,
        calls: list[ProgramCallTrace],
    ) -> tuple[EventSemantics, ReaderCard, TriageVerdict, Literal["primary", "fallback"], str | None, bool]:
        retry_available = True
        novelty_defaulted = False
        semantics: EventSemantics | None = None
        semantics_attempt = 1
        while semantics is None:
            try:
                semantics = await self._call_predictor(
                    state=self.artifact.event_semantics,
                    predictor=self.event_semantics,
                    adapter=self._resolve_adapter(adapter_pool, self.artifact.event_semantics, route),
                    route=route,
                    attempt=semantics_attempt,
                    context_sha=context_sha,
                    inputs={"evidence_json": evidence_json},
                    upstream_sha=None,
                    output_field="semantics",
                    output_model=EventSemantics,
                    calls=calls,
                )
                try:
                    _validate_semantic_context(semantics, told_count=len(context.told.entries))
                except ValueError as exc:
                    failed = calls[-1].model_copy(update={"error_code": str(exc)})
                    calls[-1] = failed
                    raise _CallFailure(
                        code=str(exc),
                        retryable=False,
                        output_failure=True,
                        finish_reason=failed.finish_reason,
                        trace=failed,
                        raw_output=semantics.model_dump(mode="json"),
                    ) from exc
            except _CallFailure as exc:
                if retry_available and exc.fast_retryable:
                    retry_available = False
                    semantics_attempt += 1
                    continue
                if (
                    exc.output_failure
                    and exc.raw_output is not None
                    and "novelty" not in exc.raw_output
                    and (exc.finish_reason or "").casefold() not in _TRUNCATED_FINISH_REASONS
                ):
                    patched = dict(exc.raw_output)
                    patched.update({"novelty": "new_fact", "restates": -1})
                    try:
                        semantics = EventSemantics.model_validate(patched)
                    except ValidationError:
                        raise exc from None
                    novelty_defaulted = True
                    patched_state = semantics.model_dump(mode="json")
                    calls[-1] = calls[-1].model_copy(
                        update={
                            "error_code": "news_program_novelty_defaulted",
                            "output_sha256": canonical_sha(patched_state),
                            "validated_output": patched_state,
                        }
                    )
                    continue
                raise
        semantics_sha = canonical_sha(semantics.model_dump(mode="json"))
        card_attempt = 1
        while True:
            try:
                card = await self._call_predictor(
                    state=self.artifact.reader_card,
                    predictor=self.reader_card,
                    adapter=self._resolve_adapter(adapter_pool, self.artifact.reader_card, route),
                    route=route,
                    attempt=card_attempt,
                    context_sha=context_sha,
                    inputs={
                        "evidence_json": evidence_json,
                        "semantics_json": canonical_json(semantics.model_dump(mode="json")),
                    },
                    upstream_sha=semantics_sha,
                    output_field="card",
                    output_model=ReaderCard,
                    calls=calls,
                )
                break
            except _CallFailure as exc:
                if retry_available and exc.fast_retryable:
                    retry_available = False
                    card_attempt += 1
                    continue
                raise
        try:
            verdict = _assemble(semantics, card, told_count=len(context.told.entries))
        except (ValidationError, ValueError) as exc:
            last = calls[-1]
            code = (
                str(exc)
                if isinstance(exc, ValueError) and str(exc).startswith("news_program_")
                else "news_program_verdict_invalid"
            )
            raise _CallFailure(
                code=code,
                retryable=False,
                output_failure=True,
                finish_reason=last.finish_reason,
                trace=last.model_copy(update={"error_code": "news_program_verdict_invalid"}),
            ) from exc
        answering_model = next(
            (call.model for call in reversed(calls) if call.route == route and call.predictor == "reader_card"),
            None,
        )
        return semantics, card, verdict, route, answering_model, novelty_defaulted

    async def _call_predictor(
        self,
        *,
        state: PredictorState,
        predictor: dspy.Predict,
        adapter: PredictorAdapter,
        route: Literal["primary", "fallback"],
        attempt: int,
        context_sha: str,
        inputs: dict[str, Any],
        upstream_sha: str | None,
        output_field: str,
        output_model: type[T],
        calls: list[ProgramCallTrace],
    ) -> T:
        model_binding = getattr(state.model_bindings, route)
        try:
            runtime_identity = adapter.runtime_identity(model_binding)
        except PredictorAdapterError as exc:
            input_sha = canonical_sha(inputs)
            request_sha = canonical_sha(
                {
                    "program_version": self.artifact.program_version,
                    "program_sha256": self.artifact.program_sha256,
                    "context_sha256": context_sha,
                    "predictor": state.name,
                    "route": route,
                    "attempt": attempt,
                    "signature_sha256": state.signature_sha256,
                    "instruction_sha256": state.instruction_sha256,
                    "demos_sha256": state.demos_sha256,
                    "adapter_sha256": self.artifact.adapter_sha256,
                    "model_binding": model_binding,
                    "runtime_identity": "unavailable",
                    "upstream_sha256": upstream_sha,
                    "inputs": inputs,
                }
            )
            call = ProgramCallTrace(
                predictor=state.name,
                route=route,
                attempt=attempt,
                request_sha256=request_sha,
                input_sha256=input_sha,
                signature_sha256=state.signature_sha256,
                instruction_sha256=state.instruction_sha256,
                demos_sha256=state.demos_sha256,
                model_binding=model_binding,
                upstream_sha256=upstream_sha,
                error_code=exc.code,
            )
            calls.append(call)
            raise _CallFailure(
                code=exc.code,
                retryable=exc.retryable,
                output_failure=exc.output_failure,
                finish_reason=exc.finish_reason,
                trace=call,
            ) from exc
        request = PredictorRequest(
            program_version=self.artifact.program_version,
            program_sha256=self.artifact.program_sha256,
            context_sha256=context_sha,
            predictor=state.name,
            route=route,
            attempt=attempt,
            signature_sha256=state.signature_sha256,
            instruction_sha256=state.instruction_sha256,
            demos_sha256=state.demos_sha256,
            adapter_sha256=self.artifact.adapter_sha256,
            model_binding=model_binding,
            runtime_provider=runtime_identity.provider,
            runtime_model=runtime_identity.model,
            runtime_model_sha256=runtime_identity.model_sha256,
            runtime_binding_sha256=runtime_identity.binding_sha256,
            upstream_sha256=upstream_sha,
            inputs=inputs,
        )
        input_sha = canonical_sha(inputs)
        adapter_started = time.perf_counter()
        try:
            response = await adapter.invoke(request, predictor)
        except asyncio.CancelledError:
            elapsed = max(0, round((time.perf_counter() - adapter_started) * 1000))
            call = ProgramCallTrace(
                predictor=state.name,
                route=route,
                attempt=attempt,
                request_sha256=request.request_sha256,
                input_sha256=input_sha,
                signature_sha256=state.signature_sha256,
                instruction_sha256=state.instruction_sha256,
                demos_sha256=state.demos_sha256,
                model_binding=request.model_binding,
                physical_provider_call=True,
                runtime_provider=request.runtime_provider,
                runtime_model=request.runtime_model,
                runtime_model_sha256=request.runtime_model_sha256,
                runtime_binding_sha256=request.runtime_binding_sha256,
                upstream_sha256=upstream_sha,
                latency_ms=elapsed,
                error_code="news_program_route_deadline",
            )
            calls.append(call)
            raise
        except PredictorAdapterError as exc:
            elapsed = max(0, round((time.perf_counter() - adapter_started) * 1000))
            observation = exc.provider_observation
            if observation is not None and (
                observation.runtime_binding_sha256 != request.runtime_binding_sha256
                or observation.provider != request.runtime_provider
            ):
                observation = None
            call = ProgramCallTrace(
                predictor=state.name,
                route=route,
                attempt=attempt,
                request_sha256=request.request_sha256,
                input_sha256=input_sha,
                signature_sha256=state.signature_sha256,
                instruction_sha256=state.instruction_sha256,
                demos_sha256=state.demos_sha256,
                model_binding=request.model_binding,
                physical_provider_call=True,
                runtime_provider=request.runtime_provider,
                runtime_model=request.runtime_model,
                runtime_model_sha256=request.runtime_model_sha256,
                runtime_binding_sha256=request.runtime_binding_sha256,
                upstream_sha256=upstream_sha,
                provider=observation.provider if observation is not None else None,
                model=observation.model if observation is not None else None,
                model_sha256=observation.model_sha256 if observation is not None else None,
                latency_ms=observation.latency_ms if observation is not None else elapsed,
                input_tokens=observation.input_tokens if observation is not None else 0,
                output_tokens=observation.output_tokens if observation is not None else 0,
                cached_tokens=observation.cached_tokens if observation is not None else 0,
                total_tokens=observation.total_tokens if observation is not None else 0,
                provider_cost_microusd=(observation.provider_cost_microusd if observation is not None else None),
                finish_reason=(observation.finish_reason if observation is not None else exc.finish_reason),
                error_code=exc.code,
            )
            calls.append(call)
            partial_raw_output: Mapping[str, Any] | None = None
            if exc.partial_output is not None:
                try:
                    partial = _unwrap_output(exc.partial_output, output_field)
                except ValueError:
                    partial = None
                if isinstance(partial, Mapping):
                    partial_raw_output = dict(partial)
            raise _CallFailure(
                code=exc.code,
                retryable=exc.retryable,
                output_failure=exc.output_failure,
                finish_reason=exc.finish_reason,
                trace=call,
                raw_output=partial_raw_output,
            ) from exc
        except Exception as exc:
            elapsed = max(0, round((time.perf_counter() - adapter_started) * 1000))
            code = f"news_program_transport_{type(exc).__name__.casefold()}"
            call = ProgramCallTrace(
                predictor=state.name,
                route=route,
                attempt=attempt,
                request_sha256=request.request_sha256,
                input_sha256=input_sha,
                signature_sha256=state.signature_sha256,
                instruction_sha256=state.instruction_sha256,
                demos_sha256=state.demos_sha256,
                model_binding=request.model_binding,
                physical_provider_call=True,
                runtime_provider=request.runtime_provider,
                runtime_model=request.runtime_model,
                runtime_model_sha256=request.runtime_model_sha256,
                runtime_binding_sha256=request.runtime_binding_sha256,
                upstream_sha256=upstream_sha,
                latency_ms=elapsed,
                error_code=code,
            )
            calls.append(call)
            raise _CallFailure(
                code=code,
                retryable=_is_retryable_exception(exc),
                output_failure=False,
                finish_reason=None,
                trace=call,
            ) from exc
        if response.runtime_binding_sha256 != request.runtime_binding_sha256:
            code = "news_program_runtime_binding_mismatch"
            call = ProgramCallTrace(
                predictor=state.name,
                route=route,
                attempt=attempt,
                request_sha256=request.request_sha256,
                input_sha256=input_sha,
                signature_sha256=state.signature_sha256,
                instruction_sha256=state.instruction_sha256,
                demos_sha256=state.demos_sha256,
                model_binding=request.model_binding,
                physical_provider_call=True,
                runtime_provider=request.runtime_provider,
                runtime_model=request.runtime_model,
                runtime_model_sha256=request.runtime_model_sha256,
                runtime_binding_sha256=request.runtime_binding_sha256,
                upstream_sha256=upstream_sha,
                provider=response.provider,
                model=response.model,
                model_sha256=response.model_sha256,
                latency_ms=response.latency_ms,
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
                cached_tokens=response.cached_tokens,
                total_tokens=response.total_tokens,
                provider_cost_microusd=response.provider_cost_microusd,
                finish_reason=response.finish_reason,
                error_code=code,
            )
            calls.append(call)
            raise _CallFailure(
                code=code,
                retryable=False,
                output_failure=False,
                finish_reason=response.finish_reason,
                trace=call,
            )
        raw_output: Mapping[str, Any] | None = None
        output_sha = canonical_sha(response.output)
        finish_reason = response.finish_reason.casefold() if response.finish_reason else None
        call = ProgramCallTrace(
            predictor=state.name,
            route=route,
            attempt=attempt,
            request_sha256=request.request_sha256,
            input_sha256=input_sha,
            signature_sha256=state.signature_sha256,
            instruction_sha256=state.instruction_sha256,
            demos_sha256=state.demos_sha256,
            model_binding=request.model_binding,
            physical_provider_call=True,
            runtime_provider=request.runtime_provider,
            runtime_model=request.runtime_model,
            runtime_model_sha256=request.runtime_model_sha256,
            runtime_binding_sha256=request.runtime_binding_sha256,
            upstream_sha256=upstream_sha,
            output_sha256=output_sha,
            provider=response.provider,
            model=response.model,
            model_sha256=response.model_sha256,
            latency_ms=response.latency_ms,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            cached_tokens=response.cached_tokens,
            total_tokens=response.total_tokens,
            provider_cost_microusd=response.provider_cost_microusd,
            finish_reason=finish_reason,
        )
        if finish_reason in _TRUNCATED_FINISH_REASONS:
            failed = call.model_copy(update={"error_code": "news_program_output_truncated"})
            calls.append(failed)
            raise _CallFailure(
                code="news_program_output_truncated",
                retryable=False,
                output_failure=True,
                finish_reason=finish_reason,
                trace=failed,
            )
        try:
            unwrapped = _unwrap_output(response.output, output_field)
            if isinstance(unwrapped, BaseModel):
                raw_output = unwrapped.model_dump(mode="json")
            elif isinstance(unwrapped, Mapping):
                raw_output = dict(unwrapped)
            else:
                raise TypeError("output_not_object")
            validated = output_model.model_validate(raw_output)
        except (TypeError, ValidationError, ValueError) as exc:
            code = f"news_program_{state.name}_invalid"
            failed = call.model_copy(update={"error_code": code})
            calls.append(failed)
            raise _CallFailure(
                code=code,
                retryable=False,
                output_failure=True,
                finish_reason=finish_reason,
                trace=failed,
                raw_output=raw_output,
            ) from exc
        validated_state = validated.model_dump(mode="json")
        calls.append(
            call.model_copy(
                update={
                    "output_sha256": canonical_sha(validated_state),
                    "validated_output": validated_state,
                }
            )
        )
        return validated

    def _trace(
        self,
        calls: Sequence[ProgramCallTrace],
        *,
        context_sha: str,
        event_semantics_sha256: str | None = None,
        reader_card_sha256: str | None = None,
        verdict_sha256: str | None = None,
        answering_route: Literal["primary", "fallback"] | None = None,
        fallback_from: str | None = None,
        novelty_defaulted: bool = False,
    ) -> ProgramTrace:
        return ProgramTrace(
            program_version=self.artifact.program_version,
            program_sha256=self.artifact.program_sha256,
            context_sha256=context_sha,
            factory_id=self.artifact.factory_id,
            topology_sha256=self.artifact.topology_sha256,
            adapter_sha256=self.artifact.adapter_sha256,
            assembler_sha256=self.artifact.assembler_sha256,
            event_semantics_sha256=event_semantics_sha256,
            reader_card_sha256=reader_card_sha256,
            verdict_sha256=verdict_sha256,
            answering_route=answering_route,
            fallback_from=fallback_from,
            novelty_defaulted=novelty_defaulted,
            calls=tuple(calls),
        )

    def _public_error(
        self,
        failure: _CallFailure,
        calls: Sequence[ProgramCallTrace],
        *,
        context_sha: str,
        primary_failure: _CallFailure | None = None,
    ) -> SemanticProgramError:
        partial_trace = self._trace(
            calls,
            context_sha=context_sha,
            fallback_from=primary_failure.code if primary_failure is not None else None,
        )
        return SemanticProgramError(
            failure.code,
            retryable=failure.retryable,
            output_failure=failure.output_failure or bool(primary_failure and primary_failure.output_failure),
            attempts=len(calls),
            partial_trace=partial_trace,
            finish_reason=failure.finish_reason,
            failing_predictor=failure.trace.predictor,
            primary_code=primary_failure.code if primary_failure is not None else None,
        )

    @staticmethod
    def _usage(calls: Sequence[ProgramCallTrace], *, started: float) -> ProgramUsage:
        return ProgramUsage(
            wall_latency_ms=max(0, round((time.perf_counter() - started) * 1000)),
            **_aggregate_program_usage(calls),
        )


__all__ = [
    "PROGRAM_DEPENDENCY_LOCK_SHA256",
    "TOLD_MAX",
    "TOLD_SAME_KEY_MAX",
    "TOLD_WINDOW_MS",
    "CompileProvenance",
    "CompileReceipt",
    "DspyCompileProgram",
    "DspyNewsSemanticProgram",
    "DspyPredictorAdapter",
    "DspyStrictJSONAdapter",
    "EventSemantics",
    "ExactMetadataDspyLM",
    "ExactProviderCallCapture",
    "ExactProviderMetadata",
    "ExecutionContract",
    "FrozenEventEvidence",
    "PredictorAdapter",
    "PredictorAdapterError",
    "PredictorModelBindings",
    "PredictorRecording",
    "PredictorRequest",
    "PredictorResponse",
    "PredictorState",
    "ProgramArtifact",
    "ProgramArtifactCodec",
    "ProgramCallTrace",
    "ProgramTrace",
    "ProgramUsage",
    "ProviderCallObservation",
    "ReaderCard",
    "RecordReplayPredictorAdapter",
    "RuntimeModelIdentity",
    "ScriptedPredictorAdapter",
    "SemanticGateContext",
    "SemanticJudge",
    "SemanticJudgment",
    "SemanticProgramError",
    "ToldLedgerEntry",
    "ToldLedgerSnapshot",
    "TriageContext",
    "load_program_artifact",
    "load_stable_program_artifact",
]
