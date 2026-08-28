"""The one module that may touch DSPy's private API.

Model transport is an Adapter Seam: everything DSPy-specific about issuing a structured request and
reading a structured answer lives here, including the private imports
(`_get_structured_outputs_response_format`, `ChatAdapter`, `openai_format`, `core.types`,
`utils.exceptions`). `graph.py` owns the graph, the budgets, the identity and the audit; it talks to a
`PredictorAdapter`, never to DSPy.

That boundary is #162 PR8's item 2, and `tests/architecture` enforces it: a DSPy import anywhere else
in `tracefold.news.program` is a failure, not a style preference.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Final, Literal, Protocol, cast, runtime_checkable

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
from pydantic import BaseModel, Field, ValidationError, model_validator

from ..artifact_identity import canonical_sha
from .runtime import _ExactModel, _reject_unsafe_state, _safe_json_state

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


class PredictorRequest(_ExactModel):
    program_version: str
    program_sha256: str
    context_sha256: str
    predictor: Literal["event_semantics", "reader_card", "progression_review"]
    route: Literal["primary", "fallback"]
    attempt: int = Field(ge=1, le=2)
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

    def parse(self, signature: type[dspy.Signature], completion: str) -> dict[str, Any]:
        try:
            return cast(dict[str, Any], super().parse(signature, completion))
        except AdapterParseError as original:
            # Some OpenAI-compatible providers intermittently follow the nested Pydantic schema itself and
            # omit DSPy's synthetic single-output envelope.  Accept only that exact, unambiguous shape: one
            # output field, a Pydantic model annotation, pure JSON, and full model validation (including the
            # model's extra-field prohibition).  Wrapped-but-invalid, multi-output and prose responses remain
            # failures so this does not weaken the Program's structured-output trust boundary.
            output_fields = signature.output_fields
            if len(output_fields) != 1:
                raise original
            output_name, output_field = next(iter(output_fields.items()))
            annotation = output_field.annotation
            if not isinstance(annotation, type) or not issubclass(annotation, BaseModel):
                raise original
            try:
                bare = json.loads(completion)
            except (TypeError, ValueError):
                raise original from None
            if not isinstance(bare, dict) or output_name in bare:
                raise original
            try:
                value = annotation.model_validate(bare)
            except ValidationError:
                raise original from None
            return {output_name: value}

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
        model_sha256: str | None = None,
    ) -> None:
        self._steps = list(steps)
        self.model_name = model_name
        self.provider = provider
        self._runtime = RuntimeModelIdentity.issue(
            provider=provider,
            model=model_name,
            model_sha256=model_sha256,
        )
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


def _safe_adapter_partial_output(
    exc: AdapterParseError | ValidationError,
    *,
    predictor: Literal["event_semantics", "reader_card", "progression_review"],
) -> dict[str, Any] | None:
    if not isinstance(exc, AdapterParseError):
        return None
    parsed = getattr(exc, "parsed_result", None)
    output_field = {
        "event_semantics": "semantics",
        "reader_card": "card",
        "progression_review": "review",
    }[predictor]
    if not isinstance(parsed, Mapping) or set(parsed) != {output_field}:
        return None
    try:
        safe = _safe_json_state(parsed)
        _reject_unsafe_state(safe, path="provider_partial_output")
    except (TypeError, ValueError):
        return None
    return cast(dict[str, Any], safe)


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
