"""The Program's own model transport: one HTTP request per Predictor call, and nothing hidden.

This module replaces `dspy_adapter.py` (#306 Phase 3). That file existed to hold DSPy's private API — the
JSON adapter's internal `_json_adapter_call_common`, `_get_structured_outputs_response_format`, the
`openai_format` conversion helpers, `core.types`, and an `LM._process_lm_response` override — because the
audit contract needed three things DSPy's public surface could not give: the ProgramTrace call count had to
equal the number of physical provider attempts, `finish_reason` had to drive the retry decision, and the
stock `JSONAdapter` silently degrades its output format and issues a *second* provider call when a parse
fails. Holding those contracts required reaching inside, and reaching inside required pinning `dspy==3.3.0`
and fencing the import to one module.

The contracts are unchanged. What changed is that they no longer need anybody's private API, because the
request is now composed here:

- one `invoke` is exactly one `POST {api_base}/chat/completions` (`integrations.chat_completions` owns the
  socket, this module owns the envelope), so `len(trace.calls)` is the number of attempts by construction
  rather than by an override that counts them;
- `finish_reason` comes off the response the caller is holding;
- there is no fallback format and no second call, because there is no second code path to fall back into;
- the route deadline, the fast retry, the fallback route and the circuit breaker stay in `graph.py`, where
  they always were.

What the model sees is a system message carrying the Predictor's complete instruction plus the output
contract, and a user message carrying the bounded fields — the untrusted Event JSON still inside its
`<tracefold-untrusted-event-json-v1>` delimiters. The reply is constrained by `response_format` built from
the output model's own JSON Schema, so the schema the code validates against and the schema the provider is
handed cannot drift.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Final, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field, model_validator

from ...integrations.chat_completions import (
    ChatTransportError,
    chat_completions_url,
    post_chat_completion,
)
from ..artifact_identity import canonical_sha
from .runtime import _ExactModel

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

# HTTP statuses that mean "ask again later" rather than "this request is wrong".
_RETRYABLE_STATUS: Final[frozenset[int]] = frozenset({408, 409, 425, 429, 500, 502, 503, 504})

# The one provider prefix this application composes (`app/llm.py: litellm_proxy_model_name`). It is a
# routing hint for a client library, not part of the model's name on the wire, and the endpoints here are
# OpenAI-compatible servers that have never heard of it. Anything else an operator writes — including a
# genuine `vendor/model` identifier — is sent through unchanged.
_WIRE_MODEL_PREFIX: Final[str] = "openai/"


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


class ProviderCallMetrics(_ExactModel):
    """Metadata normalized from exactly one OpenAI-compatible chat-completions response."""

    response_model: str | None = None
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cached_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    provider_cost_microusd: int | None = Field(default=None, ge=0)
    finish_reason: str | None = None


@dataclass(frozen=True, slots=True)
class PredictorSpec:
    """Everything the transport needs to turn one Predictor call into one HTTP request.

    Derived from the artifact by `graph.py` and handed down; the transport never reads an artifact, a
    registry or a route table of its own.
    """

    name: Literal["event_semantics", "reader_card"]
    instruction: str
    input_fields: tuple[str, ...]
    output_field: str
    output_model: type[BaseModel]
    max_tokens: int

    @property
    def output_schema(self) -> dict[str, Any]:
        return dict(self.output_model.model_json_schema())


_OUTPUT_CONTRACT: Final[str] = (
    "\n\n# OUTPUT CONTRACT\n"
    "Reply with one JSON object and nothing else: no prose, no explanation, no markdown fence. "
    'The object has exactly one key, "{field}", whose value is a {model} object matching the JSON schema '
    "supplied with this request. Every field the schema marks required must be present."
)


def system_message(instruction: str, *, output_field: str, output_model: type[BaseModel]) -> str:
    """One complete instruction plus the output contract, in that order.

    The contract is appended rather than expected inside the instruction because it describes the wire
    envelope, not the judgment: an optimizer rewriting the instruction must not be able to remove the
    sentence that says what shape the answer takes.
    """

    return f"{instruction}{_OUTPUT_CONTRACT.format(field=output_field, model=output_model.__name__)}"


def user_message(field_order: Sequence[str], values: Mapping[str, Any]) -> str:
    """The bounded input fields, each under its own heading, in a fixed order.

    Fixed order and not `values` order: the request hash is what a recording is addressed by, and a dict
    that happened to be built in a different order would address a different recording for the same call.
    """

    if set(values) != set(field_order):
        raise PredictorAdapterError("news_program_predictor_input_fields_invalid")
    return "\n\n".join(f"## {field}\n{values[field]}" for field in field_order)


def response_format(output_field: str, output_model: type[BaseModel]) -> dict[str, Any]:
    """The provider-side constraint, built from the same model the answer is validated against.

    `$defs` is hoisted to the envelope root rather than left where Pydantic put it. Both output models
    carry nested models — `TriageAsset` and `TradeRelevanceV1` — and Pydantic emits `{"$ref":
    "#/$defs/TriageAsset"}`, a pointer from the *document* root. Nesting the model's schema one level down
    without moving its definitions leaves every one of those pointers dangling, which a provider resolves
    into either an error or, worse, an unconstrained field.
    """

    schema = dict(output_model.model_json_schema())
    definitions = schema.pop("$defs", None)
    envelope: dict[str, Any] = {
        "type": "object",
        "properties": {output_field: schema},
        "required": [output_field],
        "additionalProperties": False,
    }
    if definitions:
        envelope["$defs"] = definitions
    return {"type": "json_schema", "json_schema": {"name": output_model.__name__, "schema": envelope}}


def chat_request_body(
    *,
    model: str,
    instruction: str,
    field_order: Sequence[str],
    values: Mapping[str, Any],
    output_field: str,
    output_model: type[BaseModel],
    max_tokens: int,
    temperature: float = 0,
    extras: Mapping[str, Any] | None = None,
    extra_body: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The one wire envelope every structured call in this package sends.

    Shared by the two production Predictors and by the metric judge on purpose: they ask different
    questions but they are the same kind of request, and two renderings of "one system message, one user
    message, one JSON-schema constraint" would eventually disagree about one of them.
    """

    return {
        "model": wire_model_name(model),
        "messages": [
            {
                "role": "system",
                "content": system_message(instruction, output_field=output_field, output_model=output_model),
            },
            {"role": "user", "content": user_message(field_order, values)},
        ],
        "temperature": temperature,
        "max_tokens": int(max_tokens),
        "stream": False,
        "response_format": response_format(output_field, output_model),
        **dict(extras or {}),
        **dict(extra_body or {}),
    }


def wire_model_name(model_name: str) -> str:
    """The model identifier this OpenAI-compatible endpoint expects, without the client-library prefix."""

    name = str(model_name)
    return name[len(_WIRE_MODEL_PREFIX) :] if name.startswith(_WIRE_MODEL_PREFIX) else name


class PredictorRequest(_ExactModel):
    program_version: str
    program_sha256: str
    context_sha256: str
    predictor: Literal["event_semantics", "reader_card"]
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
    """One failed Predictor call, and enough about it to charge and classify the attempt.

    `provider_reached` is what separates "the provider refused" from "the request never arrived", and the
    two settle differently: a refusal is a call the operator may be billed for even though it carries no
    usage block, while a connection that never opened costs nothing. A meter that could not tell them
    apart charged neither, so a run of 429s spent real provider work against a cost ledger that never
    moved.
    """

    def __init__(
        self,
        code: str,
        *,
        retryable: bool = False,
        output_failure: bool = False,
        finish_reason: str | None = None,
        provider_observation: ProviderCallObservation | None = None,
        provider_reached: bool = False,
    ) -> None:
        self.code = code
        self.retryable = retryable
        self.output_failure = output_failure
        self.finish_reason = finish_reason
        self.provider_observation = provider_observation
        self.provider_reached = provider_reached or provider_observation is not None
        super().__init__(code)


@runtime_checkable
class PredictorAdapter(Protocol):
    def runtime_identity(self, model_binding: str) -> RuntimeModelIdentity: ...

    async def invoke(self, request: PredictorRequest, spec: PredictorSpec) -> PredictorResponse: ...


class ChatCompletionsPredictorAdapter:
    """The production Adapter: one configured OpenAI-compatible endpoint, one request per call.

    No cache and no client-side retry, and this time by construction rather than by refusing an argument:
    there is nowhere in this class for either to live. `graph.py` owns the one fast retry, the route
    deadline, the fallback route and the primary breaker, and every physical attempt it makes appears as
    one `ProgramCallTrace`.
    """

    def __init__(
        self,
        *,
        model_name: str,
        api_key: str,
        api_base: str,
        timeout: float,
        max_tokens: int,
        model_sha256: str | None = None,
        provider: str | None = None,
        model_kwargs: Mapping[str, Any] | None = None,
        transport: Any = None,
    ) -> None:
        extras = dict(model_kwargs or {})
        # Every field this class composes. `response_format` is on the list because a caller that set it
        # through `model_kwargs` would silently drop the JSON-schema constraint the output contract depends
        # on — the answer would still fail validation in `graph.py`, but as a parse error rather than as
        # the configuration mistake it is.
        owned = {
            "api_key",
            "api_base",
            "base_url",
            "max_tokens",
            "messages",
            "model",
            "response_format",
            "stream",
            "temperature",
        }
        # `extra_body` is spread into the request body last, so its keys have to pass the same guard the
        # top-level ones do — otherwise the escape hatch quietly overrides the very fields the guard names.
        overlap = owned.intersection(set(extras) | set(dict(extras.get("extra_body") or {})))
        if overlap:
            raise ValueError(f"news_program_runtime_model_kwargs_owned:{','.join(sorted(overlap))}")
        self._model_name = str(model_name)
        self._wire_model = wire_model_name(self._model_name)
        self._api_key = str(api_key)
        self._url = chat_completions_url(api_base)
        self._timeout = float(timeout)
        self._max_tokens = int(max_tokens)
        self._transport = transport
        # `extra_body` is the OpenAI-compatible escape hatch the application already configures per model
        # family (`app/llm.py`), and its contents belong in the request body itself. Everything else is a
        # top-level body field.
        self._extra_body = dict(extras.pop("extra_body", {}) or {})
        self._extras = extras
        self._provider = provider or (
            self._model_name.split("/", maxsplit=1)[0] if "/" in self._model_name else "unknown"
        )
        self._runtime = RuntimeModelIdentity.issue(
            provider=self._provider,
            model=self._model_name,
            model_sha256=model_sha256,
        )

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
    ) -> ChatCompletionsPredictorAdapter:
        """Compose the only supported production adapter. Kept as a named constructor because the
        application seam builds four of these and names the arguments rather than the class."""

        return cls(
            model_name=model_name,
            api_key=api_key,
            api_base=api_base,
            timeout=timeout,
            max_tokens=max_tokens,
            model_sha256=model_sha256,
            provider=provider,
            model_kwargs=model_kwargs,
        )

    def runtime_identity(self, model_binding: str) -> RuntimeModelIdentity:
        del model_binding
        return self._runtime

    def request_body(self, spec: PredictorSpec, inputs: Mapping[str, Any]) -> dict[str, Any]:
        """The exact JSON body one call sends. Exposed so a test can read it without a socket."""

        return chat_request_body(
            model=self._model_name,
            instruction=spec.instruction,
            field_order=spec.input_fields,
            values=inputs,
            output_field=spec.output_field,
            output_model=spec.output_model,
            max_tokens=min(self._max_tokens, spec.max_tokens),
            extras=self._extras,
            extra_body=self._extra_body,
        )

    async def invoke(self, request: PredictorRequest, spec: PredictorSpec) -> PredictorResponse:
        if request.runtime_binding_sha256 != self._runtime.binding_sha256:
            raise PredictorAdapterError("news_program_runtime_binding_mismatch")
        body = self.request_body(spec, request.inputs)
        started = time.perf_counter()
        try:
            reply = await post_chat_completion(
                url=self._url,
                body=body,
                api_key=self._api_key,
                timeout=self._timeout,
                transport=self._transport,
            )
        except ChatTransportError as exc:
            raise PredictorAdapterError(exc.code, retryable=exc.retryable) from exc
        elapsed = max(0, round((time.perf_counter() - started) * 1000))
        if reply.status_code >= 400:
            raise PredictorAdapterError(
                f"news_program_provider_http_{reply.status_code}",
                retryable=reply.status_code in _RETRYABLE_STATUS,
                provider_reached=True,
            )
        if reply.payload is None:
            raise PredictorAdapterError("news_program_provider_body_not_json", retryable=False, provider_reached=True)
        payload = reply.payload
        metrics = provider_call_metrics(payload)
        observation = self._observation(metrics, elapsed=elapsed)
        content = choice_content(payload)
        if content is None:
            raise PredictorAdapterError(
                "news_program_provider_choice_missing",
                output_failure=True,
                finish_reason=metrics.finish_reason,
                provider_observation=observation,
            )
        try:
            parsed = json.loads(content)
        except ValueError as exc:
            code = (
                "news_program_output_truncated"
                if metrics.finish_reason in _TRUNCATED_FINISH_REASONS
                else "news_program_provider_output_not_json"
            )
            raise PredictorAdapterError(
                code,
                output_failure=True,
                finish_reason=metrics.finish_reason,
                provider_observation=observation,
            ) from exc
        if not isinstance(parsed, dict):
            raise PredictorAdapterError(
                "news_program_provider_output_not_object",
                output_failure=True,
                finish_reason=metrics.finish_reason,
                provider_observation=observation,
            )
        response_model = metrics.response_model or self._model_name
        return PredictorResponse(
            output=parsed,
            provider=self._provider,
            model=response_model,
            model_sha256=canonical_sha({"provider": self._provider, "model": response_model}),
            latency_ms=elapsed,
            input_tokens=metrics.input_tokens,
            output_tokens=metrics.output_tokens,
            cached_tokens=metrics.cached_tokens,
            total_tokens=metrics.total_tokens,
            provider_cost_microusd=metrics.provider_cost_microusd,
            finish_reason=metrics.finish_reason,
            runtime_binding_sha256=self._runtime.binding_sha256,
        )

    def _observation(self, metrics: ProviderCallMetrics, *, elapsed: int) -> ProviderCallObservation:
        model = metrics.response_model or self._model_name
        return ProviderCallObservation(
            provider=self._provider,
            model=model,
            model_sha256=canonical_sha({"provider": self._provider, "model": model}),
            latency_ms=elapsed,
            input_tokens=metrics.input_tokens,
            output_tokens=metrics.output_tokens,
            cached_tokens=metrics.cached_tokens,
            total_tokens=metrics.total_tokens,
            provider_cost_microusd=metrics.provider_cost_microusd,
            finish_reason=metrics.finish_reason,
            runtime_binding_sha256=self._runtime.binding_sha256,
        )


def choice_content(payload: Mapping[str, Any]) -> str | None:
    choices = payload.get("choices")
    if not isinstance(choices, Sequence) or not choices:
        return None
    first = choices[0]
    if not isinstance(first, Mapping):
        return None
    message = first.get("message")
    if not isinstance(message, Mapping):
        return None
    content = message.get("content")
    return content if isinstance(content, str) and content.strip() else None


def provider_call_metrics(payload: Mapping[str, Any]) -> ProviderCallMetrics:
    """Usage, finish reason and price from one chat-completions body.

    `provider_cost_microusd` stays `None` unless the body actually states a cost. Neither endpoint this
    project runs on does, and inventing a zero there would make a metered optimization run look free.
    """

    usage = payload.get("usage")
    usage_map: Mapping[str, Any] = usage if isinstance(usage, Mapping) else {}
    input_tokens = int(usage_map.get("prompt_tokens") or usage_map.get("input_tokens") or 0)
    output_tokens = int(usage_map.get("completion_tokens") or usage_map.get("output_tokens") or 0)
    total_tokens = int(usage_map.get("total_tokens") or input_tokens + output_tokens)
    cached_tokens = int(usage_map.get("cache_read_input_tokens") or 0)
    for detail_key in ("prompt_tokens_details", "input_tokens_details"):
        detail = usage_map.get(detail_key)
        if isinstance(detail, Mapping):
            cached_tokens = max(cached_tokens, int(detail.get("cached_tokens") or 0))
    finish_reason: str | None = None
    choices = payload.get("choices")
    if isinstance(choices, Sequence) and choices and isinstance(choices[0], Mapping):
        raw_reason = choices[0].get("finish_reason")
        finish_reason = str(raw_reason).casefold() if raw_reason else None
    cost_microusd = None
    raw_cost = usage_map.get("cost") if usage_map.get("cost") is not None else payload.get("cost")
    if raw_cost is not None:
        cost = Decimal(str(raw_cost))
        if not cost.is_finite() or cost < 0:
            raise ValueError("news_program_provider_cost_invalid")
        cost_microusd = int((cost * Decimal(1_000_000)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return ProviderCallMetrics(
        response_model=str(payload.get("model")) if payload.get("model") else None,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_tokens=cached_tokens,
        total_tokens=total_tokens,
        provider_cost_microusd=cost_microusd,
        finish_reason=finish_reason,
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

    async def invoke(self, request: PredictorRequest, spec: PredictorSpec) -> PredictorResponse:
        del spec
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

    async def invoke(self, request: PredictorRequest, spec: PredictorSpec) -> PredictorResponse:
        del spec
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


def _is_retryable_exception(exc: BaseException) -> bool:
    """Whether "the provider did not answer" describes this failure, for callers outside the adapter."""

    if isinstance(exc, ChatTransportError):
        return exc.retryable
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    name = type(exc).__name__.casefold()
    return any(marker in name for marker in _RETRYABLE_MARKERS)


__all__ = [
    "ChatCompletionsPredictorAdapter",
    "PredictorAdapter",
    "PredictorAdapterError",
    "PredictorRecording",
    "PredictorRequest",
    "PredictorResponse",
    "PredictorSpec",
    "ProviderCallMetrics",
    "ProviderCallObservation",
    "RecordReplayPredictorAdapter",
    "RuntimeModelIdentity",
    "ScriptedPredictorAdapter",
    "chat_request_body",
    "choice_content",
    "provider_call_metrics",
    "response_format",
    "system_message",
    "user_message",
    "wire_model_name",
]
