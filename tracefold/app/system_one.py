"""A small, auditable System One boundary for DSPy's native decision adapter."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx2
from typesafe_sdk import AsyncTypeSafeClient, RetryPolicy, TypeSafeAPIError, TypeSafeClient


@dataclass(frozen=True, slots=True)
class SystemOneReceipt:
    endpoint: str
    requested_model: str
    served_model: str | None
    request_id: str | None
    provider: str | None
    request_payload: dict[str, Any]
    response_payload: dict[str, Any] | None
    input_tokens: int | None
    output_tokens: int | None
    cost_microusd: int | None
    error_type: str | None


def _cost(raw: dict[str, Any]) -> int | None:
    usage = raw.get("usage")
    value = usage.get("cost") if isinstance(usage, dict) else raw.get("cost")
    if value is None:
        return None
    try:
        cost = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return int(cost * 1_000_000) if cost.is_finite() and cost >= 0 else None


def _answers(response: Any) -> dict[str, Any]:
    return {
        name: {
            key: getattr(answer, key)
            for key in ("noul", "score", "choice", "confidence", "probabilities")
            if hasattr(answer, key)
        }
        for name, answer in response.answers.items()
    }


def _request_id(raw: dict[str, Any] | None, headers: Mapping[str, str]) -> str | None:
    # Preserve the gateway generation ID when provided. Direct TypeSafe responses
    # identify the request in a header, not an OpenAI-shaped response body.
    for value in (
        None if raw is None else raw.get("id"),
        headers.get("x-typesafe-request-id"),
        headers.get("x-request-id"),
    ):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _receipt(
    *, endpoint: str, model: str, request: dict[str, Any], response: Any | None, error: BaseException | None
) -> SystemOneReceipt:
    raw: dict[str, Any] | None = None
    headers: Mapping[str, str] = {}
    if response is not None:
        try:
            http_response = response.raw_http_response
            headers = http_response.headers
            decoded = http_response.json()
            raw = decoded if isinstance(decoded, dict) else None
        except (AttributeError, RuntimeError, ValueError):
            raw = None
    if raw is None and response is not None:
        raw = response.model_dump(mode="json")
    if response is None and isinstance(error, TypeSafeAPIError):
        # SDK failures still carry the server's request identity. Keep the error
        # body, but never archive arbitrary headers (which can contain secrets).
        headers = error.headers or {}
        raw = error.body if isinstance(error.body, dict) else None
    usage = getattr(response, "usage", None)
    return SystemOneReceipt(
        endpoint=endpoint,
        requested_model=model,
        served_model=None if response is None else response.model,
        request_id=_request_id(raw, headers),
        provider=None if raw is None else raw.get("provider"),
        request_payload=request,
        response_payload=raw,
        input_tokens=None if usage is None else usage.input_tokens,
        output_tokens=None if usage is None else usage.output_tokens,
        cost_microusd=None if raw is None else _cost(raw),
        error_type=None if error is None else type(error).__name__,
    )


class SystemOneConnection:
    """The App owns this connection; per-Case predictors only borrow it."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float = 20,
        async_transport: httpx2.AsyncBaseTransport | None = None,
        sync_transport: httpx2.BaseTransport | None = None,
    ) -> None:
        if not all((base_url.strip(), api_key.strip(), model.strip())):
            raise ValueError("system_one_endpoint_incomplete")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._async = AsyncTypeSafeClient(
            base_url=self.base_url,
            api_key=api_key,
            model=model,
            timeout=timeout_seconds,
            retry=RetryPolicy(max_retries=0),
            transport=async_transport,
        )
        self._sync = TypeSafeClient(
            base_url=self.base_url,
            api_key=api_key,
            model=model,
            timeout=timeout_seconds,
            retry=RetryPolicy(max_retries=0),
            transport=sync_transport,
        )

    async def aclose(self) -> None:
        await self._async.aclose()
        self._sync.close()

    def bind(
        self,
        *,
        before_call: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        after_call: Callable[[SystemOneReceipt], Awaitable[None]] | None = None,
        timeout_seconds: float | None = None,
    ) -> SystemOneLM:
        return SystemOneLM(self, before_call=before_call, after_call=after_call, timeout_seconds=timeout_seconds)


class SystemOneLM:
    """DSPy sees only native state/questions and receives the SDK's answer shape."""

    supports_decision_requests = True
    cache = False

    def __init__(
        self,
        connection: SystemOneConnection,
        *,
        before_call: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        after_call: Callable[[SystemOneReceipt], Awaitable[None]] | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        self.connection = connection
        self.model = connection.model
        self.history: list[SystemOneReceipt] = []
        self._before_call = before_call
        self._after_call = after_call
        self._timeout_seconds = timeout_seconds

    def copy(self, **kwargs: Any) -> SystemOneLM:
        return SystemOneLM(
            self.connection,
            before_call=kwargs.get("before_call", self._before_call),
            after_call=kwargs.get("after_call", self._after_call),
            timeout_seconds=kwargs.get("timeout_seconds", self._timeout_seconds),
        )

    def _request(self, state: Any, questions: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(questions, Mapping) or not questions:
            raise ValueError("system_one_questions_empty")
        return {
            "phase": "jev",
            "endpoint": self.connection.base_url,
            "requested_model": self.model,
            "state": state,
            "questions": dict(questions),
        }

    async def acall(self, *, state: Any, questions: Mapping[str, Any]) -> dict[str, Any]:
        request = self._request(state, questions)
        if self._before_call is not None:
            await self._before_call(request)
        response = None
        error: BaseException | None = None
        try:
            response = await self.connection._async.system_one(
                state=state,
                questions=questions,
                timeout=self._timeout_seconds,
            )
            return _answers(response)
        except BaseException as exc:
            error = exc
            raise
        finally:
            receipt = _receipt(
                endpoint=self.connection.base_url,
                model=self.model,
                request=request,
                response=response,
                error=error,
            )
            self.history.append(receipt)
            if self._after_call is not None:
                await self._after_call(receipt)

    def __call__(self, *, state: Any, questions: Mapping[str, Any]) -> dict[str, Any]:
        if self._before_call is not None or self._after_call is not None:
            raise ValueError("system_one_sync_callbacks_unsupported")
        request = self._request(state, questions)
        response = None
        error: BaseException | None = None
        try:
            response = self.connection._sync.system_one(
                state=state,
                questions=questions,
                timeout=self._timeout_seconds,
            )
            return _answers(response)
        except BaseException as exc:
            error = exc
            raise
        finally:
            self.history.append(
                _receipt(
                    endpoint=self.connection.base_url,
                    model=self.model,
                    request=request,
                    response=response,
                    error=error,
                )
            )


def receipt_json(receipt: SystemOneReceipt) -> str:
    """A safe archive projection; never includes the connection's API key."""
    return json.dumps({key: getattr(receipt, key) for key in receipt.__dataclass_fields__}, default=str)
