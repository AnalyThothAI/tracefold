"""Bounded DSPy generation, declared fallbacks and native provider error classification."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Mapping
from typing import Any, Final

import dspy  # type: ignore[import-untyped]
from dspy.adapters.utils import serialize_for_json  # type: ignore[import-untyped]
from typesafe_sdk import TypeSafeAPIConnectionError, TypeSafeAPIError, TypeSafeAPIResponseValidationError

from ..updates.judgment import ConfigurationFault, ContractFault, ProviderUnavailable

log = logging.getLogger("tracefold.news")


ADAPTER_VERSION: Final = "news_generated_transport_v8"


_REF_FIELDS = frozenset(
    {
        "ref",
        "evidence_ref",
        "previous_ref",
        "target_ref",
        "question_ref",
        "claim_ref",
        "claim_refs",
        "focus_claim_refs",
        "antecedent_refs",
    }
)


class CompactJSONAdapter(dspy.JSONAdapter):  # type: ignore[misc]
    """DSPy's JSON adapter, showing and asking for the answer as one compact JSON line.

    DSPy renders the output skeleton and demos with `indent=2`, and the model copies that layout;
    compact answers took ~35 % fewer output tokens (#765). Only the shown format changes: the
    inherited response_format still has the server enforce the schema.
    """

    def format_field_with_value(self, fields_with_values: dict[Any, Any], role: str = "user") -> str:
        if role == "user":
            return str(super().format_field_with_value(fields_with_values, role=role))
        values = {field.name: value for field, value in fields_with_values.items()}
        return json.dumps(serialize_for_json(values), separators=(",", ":"), ensure_ascii=False)

    def user_message_output_requirements(self, signature: Any) -> str:
        requirements = super().user_message_output_requirements(signature)
        return f"{requirements} Write the JSON compactly on one line, without indentation or line breaks."


_GENERATION_TRANSIENT = (dspy.LMRateLimitError, dspy.LMServerError, dspy.LMTimeoutError, dspy.LMTransportError)


def references(value: Any, mapping: Mapping[str, str], *, field: str = "") -> Any:
    """Map reference fields only; source text and quoted prose are never rewritten."""
    if isinstance(value, dict):
        return {mapping.get(key, key): references(item, mapping, field=key) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [references(item, mapping, field=field) for item in value]
    if isinstance(value, str) and field in _REF_FIELDS:
        return mapping.get(value, value)
    return value


def _finish_reason(response: Any) -> str | None:
    """Read only structured provider metadata; never inspect or log response prose."""
    if isinstance(response, Mapping):
        reason = response.get("finish_reason")
        choices = response.get("choices")
    else:
        reason = getattr(response, "finish_reason", None)
        choices = getattr(response, "choices", None)
    if reason is not None:
        return str(reason)
    if choices:
        return _finish_reason(choices[0])
    return None


def _truncated(lm: Any, history_before: int) -> bool:
    """Whether this call's provider answer stopped at its token ceiling."""
    history: Any = getattr(lm, "history", None)
    return bool(
        history is not None
        and len(history) > history_before
        and _finish_reason(history[-1].get("response")) == "length"
    )


def _parse_failure_code(exc: dspy.AdapterParseError, lm: Any, history_before: int) -> str:
    if _truncated(lm, history_before):
        return "news_generation_output_truncated"
    if not str(exc.lm_response).strip():
        return "news_generation_output_empty"
    return "news_generation_output_schema_invalid"


def _snake(name: str) -> str:
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", "_", name).lower()


def _different_route(current: Any, fallback: Any) -> bool:
    if isinstance(current, str) or isinstance(fallback, str):
        return bool(current != fallback)

    def shape(lm: Any) -> tuple[Any, ...]:
        request = getattr(lm, "kwargs", {})
        return (
            type(lm),
            getattr(lm, "model", None),
            getattr(lm, "_structured_output", None),
            *(request.get(key) for key in ("api_base", "max_tokens", "max_completion_tokens", "temperature")),
        )

    return shape(current) != shape(fallback)


async def generate(signature: Any, route: Any, *, accept: Callable[[Any], Any] | None = None, **inputs: Any) -> Any:
    """Ask one generative signature on a configured route: the primary LM, then its declared fallback.

    A factory returns one LM or an ordered route of them. A transient provider failure
    uses the declared fallback. A malformed response uses it only if the request route
    materially differs; neither path is a second vote. The stage deadline bounds both.
    An answer the provider cut at its token ceiling is malformed even when the JSON parser
    repaired it, and so is one that `accept` (the caller's decoder of a prediction) refuses
    with a ContractFault; only the last route's refusal fails the call.
    """

    lms = tuple(route) if isinstance(route, (tuple, list)) else (route,)
    if not lms:
        raise ConfigurationFault("news_generation_route_empty")
    for index, lm in enumerate(lms):
        history_before = len(getattr(lm, "history", ()))
        try:
            # This is the normal generative signature, not a Jev probability signature
            # temporarily bound to a chat model. No global dspy.configure mutation.
            with dspy.context(adapter=CompactJSONAdapter()):
                prediction = await dspy.Predict(signature).acall(lm=lm, **inputs)
            if _truncated(lm, history_before):
                raise ContractFault("news_generation_output_truncated")
            return prediction if accept is None else accept(prediction)
        except ContractFault as exc:
            log.warning("news_generation_output_unusable code=%s route_index=%s", exc, index)
            if index + 1 < len(lms) and _different_route(lm, lms[index + 1]):
                continue
            raise
        except _GENERATION_TRANSIENT as exc:
            if index + 1 == len(lms):
                # The LM error class survives into the stored error code: `news_generation_lm_timeout_error`.
                raise ProviderUnavailable(f"news_generation_{_snake(type(exc).__name__)}") from exc
        except dspy.AdapterParseError as exc:
            code = _parse_failure_code(exc, lm, history_before)
            log.warning(
                "news_generation_output_invalid",
                extra={
                    "signature": exc.signature.__name__,
                    "adapter": exc.adapter_name,
                    "failure_code": code,
                    "response_chars": len(str(exc.lm_response)),
                    "output_fields": list(exc.signature.output_fields),
                },
            )
            if index + 1 < len(lms) and _different_route(lm, lms[index + 1]):
                continue
            raise ContractFault(code) from exc
        except ValueError as exc:
            # DSPy's decision-state decoder raises ValueError (outside AdapterParseError)
            # when a generated Score distribution is malformed. Treat that provider output
            # like another schema failure; other ValueErrors remain programming errors.
            if not str(exc).startswith("Invalid Score distribution for "):
                raise
            if index + 1 < len(lms) and _different_route(lm, lms[index + 1]):
                continue
            raise ContractFault("news_generation_output_schema_invalid") from exc
    raise ProviderUnavailable("news_generation_route_exhausted")


async def native_predict(signature: Any, lm: Any, *, code: str, **inputs: Any) -> Any:
    """One System One request through DSPy's decision adapter, with the SDK's failures classified.

    No temperature/max_tokens and no manually decoded HTTP answers.
    """

    try:
        return await dspy.Predict(signature).acall(lm=lm, **inputs)
    except TypeSafeAPIResponseValidationError as exc:
        # A successful status with an unusable body; the SDK types it as an API error.
        raise ProviderUnavailable(f"{code}_response_invalid") from exc
    except TypeSafeAPIError as exc:
        if exc.status in {401, 403}:
            raise ConfigurationFault(f"{code}_http_{exc.status}") from exc
        if exc.status == 429 or exc.status >= 500:
            raise ProviderUnavailable(f"{code}_http_{exc.status}") from exc
        raise
    except TypeSafeAPIConnectionError as exc:
        # Includes the SDK's own request timeout.
        raise ProviderUnavailable(f"{code}_{type(exc).__name__}") from exc
