"""Audited native-DSPy language-model seam for the News Program.

The module deliberately wraps DSPy's public typed LM contract instead of
reimplementing an adapter or provider transport.  One ``forward`` call is one
physical delegate invocation; DSPy's local ``JSONAdapter`` remains responsible
for rendering and parsing, while this seam owns call identity, usage, terminal
state, and safe record/replay.
"""

from __future__ import annotations

import asyncio
import math
import re
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, replace
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Final, Literal, cast

import dspy  # type: ignore[import-untyped]
from dspy.lm15 import Message, Request, Response, Usage, response_to_events  # type: ignore[import-untyped]
from dspy.utils import BaseCallback  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..artifact_identity import canonical_sha

StructuredOutputMode = Literal["json_schema", "json_object", "prompt_json"]
TerminalDisposition = Literal[
    "provider_success",
    "provider_error",
    "adapter_parse_error",
    "domain_validation_error",
    "timeout_cancelled",
    "late_completion",
]

_RECORDING_SCHEMA: Final[str] = "tracefold.news.recorded_lm.v2"
LM_REQUEST_PROJECTION_SCHEMA: Final[str] = "tracefold.news.lm_request.v2"
LM_REQUEST_IDENTITY_SCHEMA: Final[str] = "tracefold.news.audited_lm_request.v3"
_SHA_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")
_SECRET_CONFIG_KEYS: Final[frozenset[str]] = frozenset(
    {
        "api_key",
        "api_base",
        "base_url",
        "authorization",
        "headers",
        "password",
        "secret",
        "token",
    }
)
_SECRET_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{16,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{20,}\b", re.IGNORECASE),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)(https?://)[^/\s:@]+:[^@\s/]+@"),
    re.compile(
        r"(?i)\b(api[_-]?key|access[_-]?token|authorization|password|secret)\b"
        r"\s*[:=]\s*[^\s&,;]+"
    ),
)
_SAFE_ERROR_CODE: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,95}$")
_SAFE_CONFIG_FIELDS: Final[tuple[str, ...]] = (
    "temperature",
    "max_tokens",
    "top_p",
    "stop",
    "n",
    "logprobs",
    "response_format",
)


class RuntimeModelIdentity(BaseModel):
    """Secret-free identity of one configured provider route."""

    model_config = ConfigDict(extra="forbid", frozen=True)

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
                {"provider": normalized_provider, "model": normalized_model, "model_sha256": identity_sha}
            ),
        )


@dataclass(frozen=True, slots=True)
class LMCallContext:
    """Identity shared by every physical call in one Program judgment."""

    program_version: str
    program_sha256: str
    context_sha256: str
    deadline_at_monotonic: float | None = None

    def __post_init__(self) -> None:
        if not self.program_version.strip():
            raise ValueError("news_program_lm_program_version_empty")
        for name in ("program_sha256", "context_sha256"):
            if not _SHA_PATTERN.fullmatch(getattr(self, name)):
                raise ValueError(f"news_program_lm_{name}_invalid")
        if self.deadline_at_monotonic is not None and not math.isfinite(self.deadline_at_monotonic):
            raise ValueError("news_program_lm_deadline_invalid")


@dataclass(frozen=True, slots=True)
class LMCallReceipt:
    """Safe audit material for exactly one delegate invocation."""

    predictor: str
    route: str
    attempt: int
    request_sha256: str
    invocation_sha256: str
    input_sha256: str
    model_binding: str
    runtime_provider: str
    runtime_model: str
    runtime_model_sha256: str
    runtime_binding_sha256: str
    provider: str | None = None
    model: str | None = None
    model_sha256: str | None = None
    latency_ms: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_tokens: int | None = None
    total_tokens: int | None = None
    provider_cost_microusd: int | None = None
    finish_reason: str | None = None
    error_code: str | None = None
    error_detail: str | None = None
    terminal_disposition: TerminalDisposition | None = None
    recording: dict[str, Any] | None = None

    def to_program_call_trace(self) -> Any:
        """Convert a Program predictor receipt to its persistence contract."""

        if self.predictor not in {"event_semantics", "taxonomy", "reader_card"}:
            raise ValueError("news_program_lm_receipt_predictor_not_traceable")
        if self.route not in {"primary", "fallback"}:
            raise ValueError("news_program_lm_receipt_route_not_traceable")
        from .contracts import ProgramCallTrace

        usage_values = (self.input_tokens, self.output_tokens, self.cached_tokens, self.total_tokens)
        return ProgramCallTrace(
            predictor=self.predictor,
            route=self.route,
            attempt=self.attempt,
            request_sha256=self.request_sha256,
            invocation_sha256=self.invocation_sha256,
            input_sha256=self.input_sha256,
            model_binding=self.model_binding,
            physical_provider_call=True,
            runtime_provider=self.runtime_provider,
            runtime_model=self.runtime_model,
            runtime_model_sha256=self.runtime_model_sha256,
            runtime_binding_sha256=self.runtime_binding_sha256,
            provider=self.provider,
            model=self.model,
            model_sha256=self.model_sha256,
            latency_ms=self.latency_ms,
            input_tokens=self.input_tokens or 0,
            output_tokens=self.output_tokens or 0,
            cached_tokens=self.cached_tokens or 0,
            total_tokens=self.total_tokens or 0,
            usage_coverage=(
                "complete"
                if all(value is not None for value in usage_values)
                else "partial"
                if any(value is not None for value in usage_values)
                else "unknown"
            ),
            provider_cost_microusd=self.provider_cost_microusd,
            finish_reason=self.finish_reason,
            error_code=self.error_code,
            error_detail=self.error_detail,
            terminal_disposition=self.terminal_disposition,
            recording=self.recording,
        )


@dataclass(slots=True)
class _ScopeState:
    ledger: LMCallLedger
    context: LMCallContext
    start_index: int
    closed: bool = False


@dataclass(slots=True)
class _CallState:
    scope: _ScopeState
    receipt: LMCallReceipt
    request_projection: dict[str, Any]
    request_identity: dict[str, str]
    started_at: float
    answered: bool = False


_ACTIVE_SCOPE: ContextVar[_ScopeState | None] = ContextVar("tracefold_news_lm_scope", default=None)


class LMCallLedger:
    """Context-local physical-call ledger for one judgment or learning run."""

    def __init__(
        self,
        *,
        max_calls_per_predictor: int | None = None,
        max_calls_per_route: int | None = None,
        max_calls_per_scope: int | None = None,
        before_call: Callable[[], None] | None = None,
    ) -> None:
        self._limits = (max_calls_per_predictor, max_calls_per_route, max_calls_per_scope)
        if any(limit is not None and limit < 1 for limit in self._limits):
            raise ValueError("news_program_lm_call_limit_invalid")
        self._before_call = before_call
        self._calls: list[_CallState] = []

    @contextmanager
    def scope(self, context: LMCallContext) -> Iterator[LMCallLedger]:
        active = _ACTIVE_SCOPE.get()
        if active is not None and active.ledger is self:
            raise dspy.LMConfigurationError("news_program_lm_scope_nested")
        scope = _ScopeState(self, context, len(self._calls))
        token = _ACTIVE_SCOPE.set(scope)
        try:
            yield self
        except BaseException as exc:
            self._close(scope, exc)
            raise
        else:
            self._close(scope, None)
        finally:
            _ACTIVE_SCOPE.reset(token)

    @property
    def receipts(self) -> tuple[LMCallReceipt, ...]:
        return tuple(call.receipt for call in self._calls)

    @property
    def first_terminal_error(self) -> LMCallReceipt | None:
        return next(
            (
                call.receipt
                for call in self._calls
                if call.receipt.terminal_disposition not in {None, "provider_success"}
            ),
            None,
        )

    def domain_failure(self, code: str, *, predictor: str | None = None) -> LMCallReceipt:
        """Change one successful physical receipt into the domain terminal.

        ``predictor`` names the stage whose output was rejected. Without it the latest in-scope receipt is
        blamed, which is only the right answer while validation happens before the next physical call.
        #651 §5.3 breaks that: a taxonomy rejection no longer ends the route, so the ReaderCall that runs
        after it would inherit the taxonomy Predictor's failure and the audit would name the wrong stage.
        Attribution is therefore stated by the caller that knows it.
        """

        normalized = str(code).strip()
        if not normalized:
            raise ValueError("news_program_lm_domain_error_code_empty")
        active = _ACTIVE_SCOPE.get()
        candidates = self._calls if active is None or active.ledger is not self else self._calls[active.start_index :]
        if predictor is not None:
            candidates = [call for call in candidates if call.receipt.predictor == predictor]
        if not candidates:
            raise dspy.LMConfigurationError("news_program_lm_domain_failure_without_call")
        call = candidates[-1]
        disposition = call.receipt.terminal_disposition
        if disposition == "domain_validation_error" and call.receipt.error_code == normalized:
            return call.receipt
        if disposition not in {None, "provider_success"} or not call.answered:
            raise dspy.LMConfigurationError("news_program_lm_domain_failure_terminal_conflict")
        call.receipt = replace(
            call.receipt,
            terminal_disposition="domain_validation_error",
            error_code=normalized,
        )
        return call.receipt

    def latest_disposition(self, predictor: str) -> TerminalDisposition | None:
        """How the latest in-scope call of one Predictor ended, or ``None`` when it has not ended yet."""

        active = _ACTIVE_SCOPE.get()
        calls = self._calls if active is None or active.ledger is not self else self._calls[active.start_index :]
        for call in reversed(calls):
            if call.receipt.predictor == predictor:
                return call.receipt.terminal_disposition
        return None

    def late_completion(self, code: str = "news_program_route_deadline") -> LMCallReceipt:
        """Reclassify the latest answered success that crossed its route deadline."""

        normalized = str(code).strip()
        if not normalized:
            raise ValueError("news_program_lm_late_completion_code_empty")
        active = _ACTIVE_SCOPE.get()
        candidates = self._calls if active is None or active.ledger is not self else self._calls[active.start_index :]
        if not candidates:
            raise dspy.LMConfigurationError("news_program_lm_late_completion_without_call")
        call = candidates[-1]
        disposition = call.receipt.terminal_disposition
        if disposition == "late_completion" and call.receipt.error_code == normalized:
            return call.receipt
        if disposition not in {None, "provider_success"} or not call.answered:
            raise dspy.LMConfigurationError("news_program_lm_late_completion_terminal_conflict")
        call.receipt = replace(
            call.receipt,
            terminal_disposition="late_completion",
            error_code=normalized,
        )
        return call.receipt

    def _begin(
        self,
        *,
        predictor: str,
        route: str,
        model_binding: str,
        runtime: RuntimeModelIdentity,
        request_projection: dict[str, Any],
        request_identity: dict[str, str],
        request_sha256: str,
        input_sha256: str,
    ) -> _CallState:
        scope = _ACTIVE_SCOPE.get()
        if scope is None or scope.ledger is not self or scope.closed:
            raise dspy.LMConfigurationError("news_program_lm_scope_missing")

        scoped = self._calls[scope.start_index :]
        if scope.context.deadline_at_monotonic is not None and time.monotonic() >= scope.context.deadline_at_monotonic:
            if scoped and scoped[-1].answered and scoped[-1].receipt.terminal_disposition in {None, "provider_success"}:
                self.late_completion()
            raise dspy.LMTimeoutError(
                "news_program_route_deadline",
                code="news_program_route_deadline",
            )
        if scoped and scoped[-1].receipt.terminal_disposition is None and scoped[-1].answered:
            scoped[-1].receipt = replace(scoped[-1].receipt, terminal_disposition="provider_success")
        predictor_calls = [c for c in scoped if c.receipt.predictor == predictor and c.receipt.route == route]
        route_calls = [c for c in scoped if c.receipt.route == route]
        predictor_limit, route_limit, scope_limit = self._limits
        if predictor_limit is not None and len(predictor_calls) >= predictor_limit:
            raise dspy.LMConfigurationError("news_program_lm_predictor_call_budget_exhausted")
        if route_limit is not None and len(route_calls) >= route_limit:
            raise dspy.LMConfigurationError("news_program_lm_route_call_budget_exhausted")
        if scope_limit is not None and len(scoped) >= scope_limit:
            raise dspy.LMConfigurationError("news_program_lm_scope_call_budget_exhausted")
        if self._before_call is not None:
            # The caller's durable/capital admission is part of the same
            # pre-provider boundary as the local physical-call limits.  A
            # refusal therefore creates neither a receipt nor a delegate call.
            self._before_call()

        attempt = len(predictor_calls) + 1
        invocation_sha = canonical_sha(
            {
                "program_version": scope.context.program_version,
                "program_sha256": scope.context.program_sha256,
                "context_sha256": scope.context.context_sha256,
                "predictor": predictor,
                "route": route,
                "attempt": attempt,
                "model_binding": model_binding,
                "runtime_binding_sha256": runtime.binding_sha256,
                "request_sha256": request_sha256,
            }
        )
        call = _CallState(
            scope=scope,
            started_at=time.monotonic(),
            request_projection=request_projection,
            request_identity=request_identity,
            receipt=LMCallReceipt(
                predictor=predictor,
                route=route,
                attempt=attempt,
                request_sha256=request_sha256,
                invocation_sha256=invocation_sha,
                input_sha256=input_sha256,
                model_binding=model_binding,
                runtime_provider=runtime.provider,
                runtime_model=runtime.model,
                runtime_model_sha256=runtime.model_sha256,
                runtime_binding_sha256=runtime.binding_sha256,
            ),
        )
        self._calls.append(call)
        return call

    def _adapter_parse_end(self, scope: _ScopeState, exception: BaseException | None) -> None:
        for call in reversed(self._calls[scope.start_index :]):
            if call.answered and call.receipt.terminal_disposition is None:
                if exception is None:
                    call.receipt = replace(call.receipt, terminal_disposition="provider_success")
                else:
                    call.receipt = replace(
                        call.receipt,
                        terminal_disposition="adapter_parse_error",
                        error_code="news_program_adapter_parse_error",
                    )
                return

    def _close(self, scope: _ScopeState, exception: BaseException | None) -> None:
        scope.closed = True
        for call in self._calls[scope.start_index :]:
            if call.receipt.terminal_disposition is not None:
                continue
            if call.answered:
                call.receipt = replace(call.receipt, terminal_disposition="provider_success")
            elif isinstance(exception, asyncio.CancelledError):
                call.receipt = replace(
                    call.receipt,
                    terminal_disposition="timeout_cancelled",
                    error_code="news_program_lm_timeout_cancelled",
                )
            else:
                call.receipt = replace(
                    call.receipt,
                    terminal_disposition="provider_error",
                    error_code="news_program_lm_scope_abandoned",
                )


class _LedgerParseCallback(BaseCallback):  # type: ignore[misc]
    def on_adapter_parse_end(
        self,
        call_id: str,
        outputs: dict[str, Any] | None,
        exception: BaseException | None = None,
    ) -> None:
        del call_id, outputs
        scope = _ACTIVE_SCOPE.get()
        if scope is not None and not scope.closed:
            scope.ledger._adapter_parse_end(scope, exception)


_PARSE_CALLBACK: Final[_LedgerParseCallback] = _LedgerParseCallback()


def program_json_adapter() -> dspy.JSONAdapter:
    """Return the Program's local, public DSPy JSON adapter."""

    return dspy.JSONAdapter(callbacks=[_PARSE_CALLBACK], use_native_function_calling=False)


def mark_active_domain_failure(code: str, *, predictor: str | None = None) -> None:
    """Settle the answered call of ``predictor`` when native business validation rejects its output."""

    active = _ACTIVE_SCOPE.get()
    if active is not None and not active.closed:
        active.ledger.domain_failure(code, predictor=predictor)


def active_predictor_disposition(predictor: str) -> str | None:
    """How the active scope's latest call of one Predictor ended, for a caller deciding whether to degrade."""

    active = _ACTIVE_SCOPE.get()
    if active is None or active.closed:
        return None
    return active.ledger.latest_disposition(predictor)


def structured_output_capability(mode: StructuredOutputMode) -> dict[str, Any]:
    """Return the one capability map consumed by live, scripted and replay LMs."""

    return {
        "supported_params": ["response_format"] if mode != "prompt_json" else [],
        "supports_response_schema": mode == "json_schema",
    }


class LMOutputTruncatedError(dspy.LMError):  # type: ignore[misc]
    """A provider answered, but DSPy reports that its output was truncated."""

    response: Any = None


class LMDelegateProgramError(dspy.LMError):  # type: ignore[misc]
    """Carries an unknown delegate defect through DSPy's format-fallback catch boundary."""

    def __init__(self, original: Exception) -> None:
        self.original = original
        super().__init__("news_program_lm_delegate_program_error", code="program_error")


class RecordedLMMiss(dspy.LMError):  # type: ignore[misc]
    """Strict replay has no response for the actual normalized request."""


def _safe_json(value: Any, *, path: str = "request") -> Any:
    if isinstance(value, type) and issubclass(value, BaseModel):
        return _safe_json(value.model_json_schema(), path=f"{path}.schema")
    if isinstance(value, BaseModel):
        return _safe_json(value.model_dump(mode="python", exclude_none=True), path=path)
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for raw_key, child in value.items():
            key = str(raw_key)
            if key.casefold() in _SECRET_CONFIG_KEYS:
                raise dspy.LMConfigurationError(f"news_program_lm_secret_in_request:{path}.{key}")
            normalized[key] = _safe_json(child, path=f"{path}.{key}")
        return normalized
    if isinstance(value, (list, tuple)):
        return [_safe_json(child, path=f"{path}[]") for child in value]
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise dspy.LMConfigurationError(f"news_program_lm_request_not_canonical:{path}:{type(value).__name__}")


def _safe_extra_body(value: Any) -> dict[str, Any]:
    """Allow only the reviewed provider switches required by shipped routes."""

    if value in (None, {}):
        return {}
    if not isinstance(value, Mapping):
        raise dspy.LMConfigurationError("news_program_lm_extra_body_invalid")
    raw = dict(value)
    if set(raw) == {"thinking"} and isinstance(raw["thinking"], Mapping):
        thinking = dict(raw["thinking"])
        if set(thinking) == {"type"} and thinking["type"] in {"enabled", "disabled"}:
            return {"thinking": {"type": thinking["type"]}}
    if set(raw) == {"chat_template_kwargs"} and isinstance(raw["chat_template_kwargs"], Mapping):
        template = dict(raw["chat_template_kwargs"])
        if set(template) == {"enable_thinking"} and isinstance(template["enable_thinking"], bool):
            return {"chat_template_kwargs": {"enable_thinking": template["enable_thinking"]}}
    raise dspy.LMConfigurationError("news_program_lm_extra_body_unsupported")


def _reject_secret_shaped_config(value: Any, *, path: str) -> None:
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key = str(raw_key)
            if key.casefold() in _SECRET_CONFIG_KEYS:
                raise dspy.LMConfigurationError(f"news_program_lm_secret_in_request:{path}.{key}")
            _reject_secret_shaped_config(child, path=f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for child in value:
            _reject_secret_shaped_config(child, path=f"{path}[]")
    elif isinstance(value, str) and any(pattern.search(value) for pattern in _SECRET_PATTERNS):
        raise dspy.LMConfigurationError(f"news_program_lm_secret_in_request:{path}")


def _safe_config_projection(request: Request) -> dict[str, Any]:
    if request.tools:
        raise dspy.LMConfigurationError("news_program_lm_tools_unsupported")
    config = request.config
    if config.cache is not None or config.tool_choice is not None:
        raise dspy.LMConfigurationError("news_program_lm_cache_or_tools_unsupported")
    raw = asdict(config)
    allowed = {"max_tokens", "temperature", "top_p", "stop", "response_format", "logprobs", "extensions"}
    if any(value not in (None, (), {}) for key, value in raw.items() if key not in allowed):
        raise dspy.LMConfigurationError("news_program_lm_config_unsupported")
    extensions = raw.get("extensions")
    if extensions is not None:
        if not isinstance(extensions, dict) or set(extensions) != {"extra_body"}:
            raise dspy.LMConfigurationError("news_program_lm_extensions_unsupported")
        _safe_extra_body(extensions["extra_body"])
    projected = cast(dict[str, Any], _safe_json(raw, path="request.config"))
    _reject_secret_shaped_config(projected, path="request.config")
    return projected


def _validate_request_defaults(defaults: Mapping[str, Any]) -> None:
    allowed = {*_SAFE_CONFIG_FIELDS, "timeout", "extra_body"}
    unsupported = sorted(str(key) for key in defaults if key not in allowed)
    if unsupported:
        raise dspy.LMConfigurationError("news_program_lm_default_unsupported:" + ",".join(unsupported))
    _safe_extra_body(defaults.get("extra_body"))
    _reject_secret_shaped_config(defaults, path="request.defaults")


def lm_request_projection(request: Request) -> dict[str, Any]:
    """Canonical, credential-free projection of the normalized physical request."""

    return cast(
        dict[str, Any],
        _safe_json(
            {
                "schema": LM_REQUEST_PROJECTION_SCHEMA,
                "model": request.model,
                "system": request.system,
                "messages": [asdict(message) for message in request.messages],
                "tools": [],
                "config": _safe_config_projection(request),
            }
        ),
    )


def lm_request_identity(*, endpoint_fingerprint: str, model_binding: str) -> dict[str, str]:
    if not _SHA_PATTERN.fullmatch(endpoint_fingerprint):
        raise dspy.LMConfigurationError("news_program_lm_endpoint_fingerprint_invalid")
    normalized_binding = str(model_binding).strip()
    if not normalized_binding:
        raise dspy.LMConfigurationError("news_program_lm_model_binding_empty")
    return {
        "schema": LM_REQUEST_IDENTITY_SCHEMA,
        "endpoint_fingerprint": endpoint_fingerprint,
        "model_binding": normalized_binding,
    }


def lm_request_sha256(
    request: Request,
    *,
    endpoint_fingerprint: str,
    model_binding: str,
) -> str:
    projection = lm_request_projection(request)
    identity = lm_request_identity(
        endpoint_fingerprint=endpoint_fingerprint,
        model_binding=model_binding,
    )
    return canonical_sha({**identity, "request": projection})


def _scrub_detail(value: str) -> str | None:
    detail = str(value).strip()
    if not detail:
        return None
    for pattern in _SECRET_PATTERNS:
        replacement = r"\1[redacted]@" if pattern.pattern.startswith("(?i)(https?://)") else "[redacted]"
        detail = pattern.sub(replacement, detail)
    encoded = detail.encode("utf-8")
    if len(encoded) <= 200:
        return detail
    return encoded[:200].decode("utf-8", errors="ignore")


def _usage_values(response: Response) -> tuple[int | None, int | None, int | None, int | None]:
    usage = response.usage
    return usage.input_tokens, usage.output_tokens, usage.cache_read_tokens, usage.total_tokens


def physical_call_usage(response: Any) -> dict[str, int | None]:
    """Tokens and observed provider cost for one delegate response, in the ledger's own units.

    `LMCallLedger.scope` is a *Program judgment* scope: it is ContextVar-local, requires an
    `LMCallContext` (program version, program sha, context sha) and its receipts convert to
    `ProgramCallTrace` for the three Program Predictors only. A review drafter is a metric-side tool that
    runs off-Program and, since #675 §4, inside worker threads where that ContextVar is not set. It
    therefore reuses this accounting rather than opening a Program scope it is not part of, so there is
    still exactly one place that reads a provider's usage and cost.

    `cost_microusd` is `None` when the provider did not state a cost; an unknown cost is never zero.
    """

    input_tokens, output_tokens, cached_tokens, total_tokens = _usage_values(cast(Any, response))
    provider_data = getattr(response, "provider_data", None)
    raw_cost = provider_data.get("cost") if isinstance(provider_data, Mapping) else None
    try:
        cost = _cost_microusd(float(raw_cost) if raw_cost is not None else None)
    except (dspy.LMUnexpectedError, TypeError, ValueError):
        cost = None
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cached_tokens": cached_tokens,
        "total_tokens": total_tokens,
        "cost_microusd": cost,
    }


def _cost_microusd(cost: float | None) -> int | None:
    if cost is None:
        return None
    value = Decimal(str(cost))
    if not value.is_finite() or value < 0:
        raise dspy.LMUnexpectedError("news_program_lm_provider_cost_invalid")
    return int((value * Decimal(1_000_000)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _recorded_response(response: Response, runtime: RuntimeModelIdentity) -> dict[str, Any]:
    input_tokens, output_tokens, cached_tokens, total_tokens = _usage_values(response)
    text = response.text
    if text is None:
        raise dspy.LMUnexpectedError("news_program_lm_response_text_missing")
    return {
        "model": response.model,
        "text": text,
        "finish_reason": _scrub_detail(response.finish_reason),
        "truncated": response.finish_reason == "length",
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "cache_read_tokens": cached_tokens,
        },
        "cost": (response.provider_data or {}).get("cost"),
    }


def _safe_status(value: int | None) -> int | None:
    return value if isinstance(value, int) and 100 <= value <= 599 else None


def _safe_retry_after(value: float | None) -> float | None:
    if value is None:
        return None
    normalized = float(value)
    return normalized if math.isfinite(normalized) and 0 <= normalized <= 86_400 else None


def _stable_error_code(exc: dspy.LMError) -> str:
    raw = str(exc.code or "").strip()
    if raw.startswith("news_program_") and _SAFE_ERROR_CODE.fullmatch(raw):
        return raw
    if _SAFE_ERROR_CODE.fullmatch(raw) and not any(pattern.search(raw) for pattern in _SECRET_PATTERNS):
        return f"news_program_lm_{raw}"
    return _error_code(exc)


def _sanitized_lm_error(exc: dspy.LMError, runtime: RuntimeModelIdentity) -> dspy.LMError:
    error_type = type(exc) if type(exc).__name__ in _ERROR_TYPES else dspy.LMUnexpectedError
    return error_type(
        _scrub_detail(exc.message) or "language model error",
        code=_stable_error_code(exc),
        model=runtime.model,
        provider=runtime.provider,
        provider_code=None,
        status=_safe_status(exc.status),
        retry_after=_safe_retry_after(exc.retry_after),
    )


def _recorded_error(exc: dspy.LMError, runtime: RuntimeModelIdentity) -> dict[str, Any]:
    return {
        "type": type(exc).__name__,
        "message": _scrub_detail(exc.message) or "language model error",
        "code": _stable_error_code(exc),
        "model": runtime.model,
        "provider": runtime.provider,
        "provider_code": None,
        "status": _safe_status(exc.status),
        "retry_after": _safe_retry_after(exc.retry_after),
    }


def _recording(
    projection: dict[str, Any],
    request_identity: dict[str, str],
    request_sha: str,
    *,
    runtime: RuntimeModelIdentity,
    response: Response | None = None,
    error: dspy.LMError | None = None,
) -> dict[str, Any]:
    return {
        "schema": _RECORDING_SCHEMA,
        "request_sha256": request_sha,
        "request_identity": request_identity,
        "request": projection,
        "response": None if response is None else _recorded_response(response, runtime),
        "error": None if error is None else _recorded_error(error, runtime),
    }


class _AuditedSyncEngine:
    def __init__(self, owner: AuditedConfiguredLM) -> None:
        self.owner = owner

    def complete(self, request: Request) -> Response:
        return self.owner._complete(request)

    def stream(self, request: Request) -> Any:
        return response_to_events(self.complete(request))

    def close(self) -> None:
        self.owner._delegate.close()


class _AuditedAsyncEngine:
    def __init__(self, owner: AuditedConfiguredLM) -> None:
        self.owner = owner

    async def complete(self, request: Request) -> Response:
        return await self.owner._acomplete(request)

    async def stream(self, request: Request) -> Any:
        for event in response_to_events(await self.complete(request)):
            yield event

    async def aclose(self) -> None:
        await self.owner._delegate.aclose()


class AuditedConfiguredLM(dspy.LM):  # type: ignore[misc]
    """Native LM with a one-attempt, canonical Request/Response audit engine."""

    def __init__(
        self,
        delegate: dspy.LM,
        *,
        structured_output: StructuredOutputMode,
        runtime_identity: RuntimeModelIdentity,
        predictor: str,
        route: str,
        model_binding: str,
        ledger: LMCallLedger | None = None,
        request_kwargs: Mapping[str, Any] | None = None,
    ) -> None:
        if delegate.cache is not False or delegate.num_retries != 0:
            raise dspy.LMConfigurationError("news_program_lm_delegate_must_disable_cache_and_retries")
        if delegate.model != runtime_identity.model:
            raise dspy.LMConfigurationError("news_program_lm_runtime_model_mismatch")
        if not predictor.strip() or not route.strip() or not model_binding.strip():
            raise dspy.LMConfigurationError("news_program_lm_role_identity_empty")
        defaults = {
            key: value
            for key, value in delegate.kwargs.items()
            if key.casefold() not in _SECRET_CONFIG_KEYS and key != "timeout" and value is not None
        }
        requested = dict(request_kwargs or {})
        if any(str(key).casefold() in _SECRET_CONFIG_KEYS for key in requested):
            raise dspy.LMConfigurationError("news_program_lm_secret_in_request_kwargs")
        defaults.update(requested)
        self._extra_body = _safe_extra_body(defaults.pop("extra_body", None))
        _validate_request_defaults(defaults)
        self._delegate = delegate
        self._structured_output = structured_output
        self.runtime_identity = runtime_identity
        self.predictor = predictor
        self.route = route
        self.model_binding = model_binding
        self.ledger = ledger
        super().__init__(
            delegate.model,
            cache=False,
            num_retries=0,
            engine=_AuditedSyncEngine(self),
            async_engine=_AuditedAsyncEngine(self),
            **defaults,
        )
        replay_identity = getattr(delegate, "runtime_identity", None)
        replay_binding = getattr(delegate, "model_binding", None)
        if replay_identity is not None and (replay_identity != runtime_identity or replay_binding != model_binding):
            raise dspy.LMConfigurationError("news_program_lm_replay_identity_mismatch")

    def copy(self, **kwargs: Any) -> AuditedConfiguredLM:
        delegate = self._delegate.copy(**kwargs)
        return AuditedConfiguredLM(
            delegate,
            structured_output=self._structured_output,
            runtime_identity=self.runtime_identity,
            predictor=self.predictor,
            route=self.route,
            model_binding=self.model_binding,
            ledger=self.ledger,
        )

    @property
    def supported_params(self) -> set[str]:
        return set(structured_output_capability(self._structured_output)["supported_params"])

    @property
    def supports_response_schema(self) -> bool:
        return bool(structured_output_capability(self._structured_output)["supports_response_schema"])

    def _with_provider_extensions(self, request: Request) -> Request:
        if not self._extra_body:
            return request
        if request.config.extensions:
            raise dspy.LMConfigurationError("news_program_lm_extensions_conflict")
        return replace(
            request,
            config=replace(request.config, extensions={"extra_body": self._extra_body}),
        )

    def _complete(self, request: Request) -> Response:
        request = self._with_provider_extensions(request)
        projection, request_sha, ledger, call = self._start(request)
        try:
            response = self._delegate(request)
            if not isinstance(response, Response):
                raise dspy.LMUnexpectedError("news_program_lm_delegate_response_invalid")
            self._answered(call, response, projection, request_sha)
            self._raise_if_truncated(response)
            return response
        except asyncio.CancelledError:
            self._failed(call, dspy.LMTimeoutError("news_program_lm_timeout_cancelled"), "timeout_cancelled")
            raise
        except LMOutputTruncatedError as exc:
            replayed = getattr(exc, "response", None)
            if isinstance(replayed, Response) and not call.answered:
                self._answered(call, replayed, projection, request_sha)
            raise
        except dspy.LMUnexpectedError as exc:
            if isinstance(exc.__cause__, Exception):
                raise LMDelegateProgramError(exc.__cause__) from None
            raise self._failed(call, exc, "provider_error") from None
        except dspy.LMError as exc:
            if call.receipt.terminal_disposition is None:
                sanitized = self._failed(call, exc, "provider_error")
            else:
                sanitized = _sanitized_lm_error(exc, self.runtime_identity)
            raise sanitized from None
        except Exception as exc:
            raise LMDelegateProgramError(exc) from None
        finally:
            del ledger

    async def _acomplete(self, request: Request) -> Response:
        request = self._with_provider_extensions(request)
        projection, request_sha, ledger, call = self._start(request)
        try:
            response = await self._delegate.acall(request)
            if not isinstance(response, Response):
                raise dspy.LMUnexpectedError("news_program_lm_delegate_response_invalid")
            self._answered(call, response, projection, request_sha)
            self._raise_if_truncated(response)
            return response
        except asyncio.CancelledError:
            self._failed(call, dspy.LMTimeoutError("news_program_lm_timeout_cancelled"), "timeout_cancelled")
            raise
        except LMOutputTruncatedError as exc:
            replayed = getattr(exc, "response", None)
            if isinstance(replayed, Response) and not call.answered:
                self._answered(call, replayed, projection, request_sha)
            raise
        except dspy.LMUnexpectedError as exc:
            if isinstance(exc.__cause__, Exception):
                raise LMDelegateProgramError(exc.__cause__) from None
            raise self._failed(call, exc, "provider_error") from None
        except dspy.LMError as exc:
            if call.receipt.terminal_disposition is None:
                sanitized = self._failed(call, exc, "provider_error")
            else:
                sanitized = _sanitized_lm_error(exc, self.runtime_identity)
            raise sanitized from None
        except Exception as exc:
            raise LMDelegateProgramError(exc) from None
        finally:
            del ledger

    def _start(self, request: Request) -> tuple[dict[str, Any], str, LMCallLedger, _CallState]:
        projection = lm_request_projection(request)
        request_identity = lm_request_identity(
            endpoint_fingerprint=self.runtime_identity.model_sha256,
            model_binding=self.model_binding,
        )
        request_sha = canonical_sha({**request_identity, "request": projection})
        input_sha = canonical_sha({"messages": projection["messages"]})
        active = _ACTIVE_SCOPE.get()
        ledger = self.ledger or (None if active is None else active.ledger)
        if ledger is None:
            raise dspy.LMConfigurationError("news_program_lm_ledger_missing")
        call = ledger._begin(
            predictor=self.predictor,
            route=self.route,
            model_binding=self.model_binding,
            runtime=self.runtime_identity,
            request_projection=projection,
            request_identity=request_identity,
            request_sha256=request_sha,
            input_sha256=input_sha,
        )
        return projection, request_sha, ledger, call

    def _answered(
        self,
        call: _CallState,
        response: Response,
        projection: dict[str, Any],
        request_sha: str,
    ) -> None:
        input_tokens, output_tokens, cached_tokens, total_tokens = _usage_values(response)
        model = response.model or self.runtime_identity.model
        call.answered = True
        disposition: TerminalDisposition | None = "late_completion" if call.scope.closed else None
        error_code = None
        if response.finish_reason == "length" and disposition is None:
            disposition = "provider_success"
            error_code = "news_program_lm_output_truncated"
        call.receipt = replace(
            call.receipt,
            provider=self.runtime_identity.provider,
            model=model,
            model_sha256=canonical_sha({"provider": self.runtime_identity.provider, "model": model}),
            latency_ms=max(0, round((time.monotonic() - call.started_at) * 1000)),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_tokens=cached_tokens,
            total_tokens=total_tokens,
            provider_cost_microusd=physical_call_usage(response)["cost_microusd"],
            finish_reason=_scrub_detail(response.finish_reason),
            error_code=error_code,
            terminal_disposition=disposition,
            recording=_recording(
                projection,
                call.request_identity,
                request_sha,
                runtime=self.runtime_identity,
                response=response,
            ),
        )

    def _failed(
        self,
        call: _CallState,
        exc: dspy.LMError,
        disposition: TerminalDisposition,
    ) -> dspy.LMError:
        sanitized = _sanitized_lm_error(exc, self.runtime_identity)
        call.receipt = replace(
            call.receipt,
            latency_ms=max(0, round((time.monotonic() - call.started_at) * 1000)),
            error_code=sanitized.code or _error_code(sanitized),
            error_detail=_scrub_detail(sanitized.message),
            terminal_disposition=disposition,
            recording=_recording(
                call.request_projection,
                call.request_identity,
                call.receipt.request_sha256,
                runtime=self.runtime_identity,
                error=sanitized,
            ),
        )
        return sanitized

    @staticmethod
    def _raise_if_truncated(response: Response) -> None:
        if response.finish_reason == "length":
            raise LMOutputTruncatedError(
                "news_program_lm_output_truncated",
                code="news_program_lm_output_truncated",
                model=response.model,
            )


def _error_code(exc: dspy.LMError) -> str:
    return f"news_program_lm_{type(exc).default_code}"


type ScriptedStep = Any


class _ScriptedSyncEngine:
    def __init__(self, owner: ScriptedLM) -> None:
        self.owner = owner

    def complete(self, request: Request) -> Response:
        return self.owner._next(request)

    def stream(self, request: Request) -> Any:
        return response_to_events(self.complete(request))

    def close(self) -> None:
        pass


class _ScriptedAsyncEngine:
    def __init__(self, owner: ScriptedLM) -> None:
        self.owner = owner

    async def complete(self, request: Request) -> Response:
        return self.owner._next(request)

    async def stream(self, request: Request) -> Any:
        for event in response_to_events(self.owner._next(request)):
            yield event

    async def aclose(self) -> None:
        pass


class ScriptedLM(dspy.LM):  # type: ignore[misc]
    """Ordered canonical test engine behind the public DSPy LM entry."""

    def __init__(
        self,
        steps: Sequence[ScriptedStep],
        *,
        model: str = "scripted/test",
        structured_output: StructuredOutputMode = "json_schema",
        **kwargs: Any,
    ) -> None:
        cache = kwargs.pop("cache", False)
        num_retries = kwargs.pop("num_retries", 0)
        self._steps = list(steps)
        self._structured_output = structured_output
        self.requests: list[Request] = []
        super().__init__(
            model,
            cache=cache,
            num_retries=num_retries,
            engine=_ScriptedSyncEngine(self),
            async_engine=_ScriptedAsyncEngine(self),
            **kwargs,
        )

    def copy(self, **kwargs: Any) -> ScriptedLM:
        defaults = {**self.kwargs, **kwargs}
        return ScriptedLM(
            list(self._steps),
            model=self.model,
            structured_output=self._structured_output,
            **defaults,
        )

    @property
    def supported_params(self) -> set[str]:
        return set(structured_output_capability(self._structured_output)["supported_params"])

    @property
    def supports_response_schema(self) -> bool:
        return bool(structured_output_capability(self._structured_output)["supports_response_schema"])

    def _next(self, request: Request) -> Response:
        self.requests.append(request)
        if not self._steps:
            raise dspy.LMUnexpectedError("news_program_lm_script_exhausted")
        step = self._steps.pop(0)
        if callable(step):
            step = step(request)
        if isinstance(step, BaseException):
            raise step
        if isinstance(step, Response):
            return step
        text = step if isinstance(step, str) else _json_text(step)
        return Response(
            id=None,
            model=self.model,
            message=Message.assistant(text),
            finish_reason="stop",
            usage=Usage(input_tokens=0, output_tokens=0, total_tokens=0, cache_read_tokens=0),
        )


def _json_text(value: Mapping[str, Any]) -> str:
    from ..artifact_identity import canonical_json

    return canonical_json(value)


_ERROR_TYPES: Final[dict[str, type[dspy.LMError]]] = {
    cls.__name__: cls
    for cls in (
        dspy.LMAuthError,
        dspy.LMBillingError,
        dspy.LMConfigurationError,
        dspy.LMInvalidRequestError,
        dspy.LMNotConfiguredError,
        dspy.LMProviderError,
        dspy.LMRateLimitError,
        dspy.LMServerError,
        dspy.LMTimeoutError,
        dspy.LMTransportError,
        dspy.LMUnexpectedError,
        dspy.LMUnsupportedFeatureError,
        dspy.LMUnsupportedModelError,
        LMOutputTruncatedError,
    )
}


class _RequestIdentityModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_name: Literal["tracefold.news.audited_lm_request.v3"] = Field(alias="schema")
    endpoint_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    model_binding: str = Field(min_length=1)


class _RecordedUsageModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    input_tokens: int | None = Field(default=None, ge=0, strict=True)
    output_tokens: int | None = Field(default=None, ge=0, strict=True)
    total_tokens: int | None = Field(default=None, ge=0, strict=True)
    cache_read_tokens: int | None = Field(default=None, ge=0, strict=True)


class _RecordedResponseModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    model: str = Field(min_length=1)
    text: str
    finish_reason: str | None
    truncated: bool = Field(strict=True)
    usage: _RecordedUsageModel
    cost: float | None = Field(default=None, ge=0, allow_inf_nan=False, strict=True)

    @field_validator("finish_reason")
    @classmethod
    def _finish_reason_is_bounded(cls, value: str | None) -> str | None:
        if value is not None and len(value.encode("utf-8")) > 200:
            raise ValueError("news_program_recording_finish_reason_oversized")
        return value


class _RecordedErrorModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: str = Field(min_length=1)
    message: str = Field(min_length=1)
    code: str = Field(pattern=r"^[a-z0-9][a-z0-9_.:-]{0,95}$")
    model: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    provider_code: None = None
    status: int | None = Field(default=None, ge=100, le=599, strict=True)
    retry_after: float | None = Field(default=None, ge=0, le=86_400, allow_inf_nan=False, strict=True)

    @model_validator(mode="after")
    def _known_and_bounded(self) -> _RecordedErrorModel:
        if self.type not in _ERROR_TYPES:
            raise ValueError("news_program_recording_error_type_unsupported")
        if len(self.message.encode("utf-8")) > 200:
            raise ValueError("news_program_recording_error_message_oversized")
        return self


class _RecordingModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_name: Literal["tracefold.news.recorded_lm.v2"] = Field(alias="schema")
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_identity: _RequestIdentityModel
    request: dict[str, Any]
    response: _RecordedResponseModel | None
    error: _RecordedErrorModel | None

    @model_validator(mode="after")
    def _one_terminal(self) -> _RecordingModel:
        if (self.response is None) == (self.error is None):
            raise ValueError("news_program_recording_terminal_invalid")
        identity = lm_request_identity(
            endpoint_fingerprint=self.request_identity.endpoint_fingerprint,
            model_binding=self.request_identity.model_binding,
        )
        if self.request_identity.model_dump(mode="json", by_alias=True) != identity:
            raise ValueError("news_program_recording_request_identity_invalid")
        if canonical_sha({**identity, "request": self.request}) != self.request_sha256:
            raise ValueError("news_program_recording_request_identity_mismatch")
        return self


class _RecordedSyncEngine:
    def __init__(self, owner: RecordedLM) -> None:
        self.owner = owner

    def complete(self, request: Request) -> Response:
        return self.owner._replay(request)

    def stream(self, request: Request) -> Any:
        return response_to_events(self.complete(request))

    def close(self) -> None:
        pass


class _RecordedAsyncEngine:
    def __init__(self, owner: RecordedLM) -> None:
        self.owner = owner

    async def complete(self, request: Request) -> Response:
        return self.owner._replay(request)

    async def stream(self, request: Request) -> Any:
        for event in response_to_events(self.owner._replay(request)):
            yield event

    async def aclose(self) -> None:
        pass


class RecordedLM(dspy.LM):  # type: ignore[misc]
    """Strict canonical request-addressed replay with no live fallback."""

    def __init__(
        self,
        recordings: Mapping[str, Mapping[str, Any]],
        *,
        model: str,
        runtime_identity: RuntimeModelIdentity,
        model_binding: str,
        structured_output: StructuredOutputMode = "json_schema",
    ) -> None:
        if model != runtime_identity.model:
            raise ValueError("news_program_recording_runtime_model_mismatch")
        self.runtime_identity = runtime_identity
        self.model_binding = str(model_binding).strip()
        expected_identity = lm_request_identity(
            endpoint_fingerprint=runtime_identity.model_sha256,
            model_binding=self.model_binding,
        )
        parsed: dict[str, _RecordingModel] = {}
        for key, value in recordings.items():
            if value.get("schema") != _RECORDING_SCHEMA:
                raise ValueError("news_program_recording_schema_unsupported")
            recording = _RecordingModel.model_validate(value)
            if key != recording.request_sha256:
                raise ValueError("news_program_recording_key_identity_mismatch")
            if recording.request_identity.model_dump(mode="json", by_alias=True) != expected_identity:
                raise ValueError("news_program_recording_runtime_identity_mismatch")
            if recording.request.get("model") != model:
                raise ValueError("news_program_recording_runtime_model_mismatch")
            parsed[key] = recording
        self._recordings = parsed
        self._structured_output = structured_output
        self.requests: list[Request] = []
        super().__init__(
            model,
            cache=False,
            num_retries=0,
            engine=_RecordedSyncEngine(self),
            async_engine=_RecordedAsyncEngine(self),
        )

    def copy(self, **kwargs: Any) -> RecordedLM:
        if kwargs:
            raise ValueError("news_program_recording_copy_config_change_unsupported")
        return RecordedLM(
            {key: value.model_dump(mode="json", by_alias=True) for key, value in self._recordings.items()},
            model=self.model,
            runtime_identity=self.runtime_identity,
            model_binding=self.model_binding,
            structured_output=self._structured_output,
        )

    @property
    def supported_params(self) -> set[str]:
        return set(structured_output_capability(self._structured_output)["supported_params"])

    @property
    def supports_response_schema(self) -> bool:
        return bool(structured_output_capability(self._structured_output)["supports_response_schema"])

    def _replay(self, request: Request) -> Response:
        self.requests.append(request)
        request_sha = lm_request_sha256(
            request,
            endpoint_fingerprint=self.runtime_identity.model_sha256,
            model_binding=self.model_binding,
        )
        recording = self._recordings.get(request_sha)
        if recording is None:
            raise RecordedLMMiss(
                "news_program_recording_missing",
                code="news_program_recording_missing",
                model=request.model,
            )
        if recording.request != lm_request_projection(request):
            raise RecordedLMMiss("news_program_recording_request_mismatch")
        if recording.error is not None:
            raise _replayed_error(recording.error)
        raw = recording.response
        if raw is None:  # Guard for type checkers; the model validator rejects this state.
            raise ValueError("news_program_recording_terminal_invalid")
        response = Response(
            id=None,
            model=raw.model,
            message=Message.assistant(raw.text),
            finish_reason="length" if raw.truncated else raw.finish_reason or "stop",
            usage=Usage(**raw.usage.model_dump(mode="json")),
            provider_data={"cost": raw.cost} if raw.cost is not None else None,
        )
        if raw.truncated:
            error = LMOutputTruncatedError(
                "news_program_lm_output_truncated",
                code="news_program_lm_output_truncated",
                model=response.model,
            )
            # An audited outer LM can preserve the original answered usage and
            # disposition while direct replay still raises before JSONAdapter
            # can mistake truncation for a format fallback.
            error.response = response
            raise error
        return response


def _replayed_error(raw: _RecordedErrorModel) -> dspy.LMError:
    error_type = _ERROR_TYPES.get(raw.type)
    if error_type is None:
        raise ValueError("news_program_recording_error_type_unsupported")
    return error_type(
        raw.message,
        code=raw.code,
        model=raw.model,
        provider=raw.provider,
        provider_code=None,
        status=raw.status,
        retry_after=raw.retry_after,
    )


__all__ = [
    "LM_REQUEST_IDENTITY_SCHEMA",
    "LM_REQUEST_PROJECTION_SCHEMA",
    "AuditedConfiguredLM",
    "LMCallContext",
    "LMCallLedger",
    "LMCallReceipt",
    "LMDelegateProgramError",
    "LMOutputTruncatedError",
    "RecordedLM",
    "RecordedLMMiss",
    "RuntimeModelIdentity",
    "ScriptedLM",
    "StructuredOutputMode",
    "TerminalDisposition",
    "active_predictor_disposition",
    "lm_request_identity",
    "lm_request_projection",
    "lm_request_sha256",
    "mark_active_domain_failure",
    "physical_call_usage",
    "program_json_adapter",
    "structured_output_capability",
]
