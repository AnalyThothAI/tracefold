"""The one HTTP client for OpenAI-compatible `chat/completions`, and nothing else.

`tests/architecture/test_external_data_runtime_contract.py` keeps provider network runtimes out of the
business packages: `httpx` belongs to an integration, so that a business module can be read, reasoned about
and tested without a socket, and so that provider transport quirks live in one place rather than three.

#306 Phase 3 gave the News Program its own model transport, which made that rule bite for the first time —
before it, DSPy owned the socket and the rule was satisfied by accident. The split follows the rule's own
logic. What is *not* here is everything the audit contract is made of: the request envelope, the JSON-schema
constraint, `finish_reason`, the usage and cost projection, the retry decision, the route deadline and the
circuit breaker. Those are `tracefold.news.program.transport` and `tracefold.news.program.graph`, because
they are claims about the Program, not about HTTP.

So this module knows exactly two things: how to send one JSON body with a bearer credential, and how to tell
"the provider never answered" apart from "the provider answered with a status". Both callers — the two
Predictors and the metric judge — read the reply and decide what it means.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

import httpx

_RETRYABLE_MARKERS: Final[tuple[str, ...]] = (
    "timeout",
    "connect",
    "read",
    "network",
    "pool",
    "remoteprotocol",
)


@dataclass(frozen=True, slots=True)
class ChatCompletionReply:
    """One provider answer, undecoded beyond JSON. `payload` is `None` when the body was not JSON."""

    status_code: int
    payload: Mapping[str, Any] | None


class ChatTransportError(Exception):
    """The provider never returned a response: nothing reported usage, and nothing was billed.

    Kept distinct from a status code because the two settle differently. A refused request has a body and
    a cost; a request that never arrived has neither, and charging one would invent spend.
    """

    def __init__(self, code: str, *, retryable: bool) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(code)


def chat_completions_url(api_base: str) -> str:
    return f"{str(api_base).rstrip('/')}/chat/completions"


def _headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}


def _reply(response: httpx.Response) -> ChatCompletionReply:
    try:
        payload = response.json()
    except ValueError:
        return ChatCompletionReply(status_code=response.status_code, payload=None)
    return ChatCompletionReply(
        status_code=response.status_code,
        payload=payload if isinstance(payload, Mapping) else None,
    )


def _transport_error(exc: httpx.HTTPError) -> ChatTransportError:
    name = type(exc).__name__.casefold()
    return ChatTransportError(
        f"news_program_transport_{name}",
        retryable=isinstance(exc, (httpx.TimeoutException, httpx.TransportError))
        or any(marker in name for marker in _RETRYABLE_MARKERS),
    )


async def post_chat_completion(
    *,
    url: str,
    body: Mapping[str, Any],
    api_key: str,
    # The HTTP client's own deadline, not an `asyncio.timeout`: cancelling mid-request would lose the
    # distinction between "the provider refused" and "we stopped listening", and the Program's route
    # deadline is a separate, wider bound owned by `graph.py`.
    timeout: float,  # noqa: ASYNC109
    transport: httpx.AsyncBaseTransport | None = None,
) -> ChatCompletionReply:
    """One request, one response. No cache, no retry, no follow-up call — by construction, not by setting.

    `timeout` is the HTTP client's own, which is what the role identity attests and what the provider is
    held to; the Program's route deadline is a separate, wider bound owned by `graph.py`. An
    `asyncio.timeout` here would cancel mid-request and lose the distinction between "the provider refused"
    and "we stopped listening".
    """

    try:
        async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:
            response = await client.post(url, json=dict(body), headers=_headers(api_key))
    except httpx.HTTPError as exc:
        raise _transport_error(exc) from exc
    return _reply(response)


def post_chat_completion_sync(
    *,
    url: str,
    body: Mapping[str, Any],
    api_key: str,
    timeout: float,
    transport: httpx.BaseTransport | None = None,
) -> ChatCompletionReply:
    """The same call for the two synchronous callers: the metric judge and the reflection role."""

    try:
        with httpx.Client(timeout=timeout, transport=transport) as client:
            response = client.post(url, json=dict(body), headers=_headers(api_key))
    except httpx.HTTPError as exc:
        raise _transport_error(exc) from exc
    return _reply(response)


__all__ = [
    "ChatCompletionReply",
    "ChatTransportError",
    "chat_completions_url",
    "post_chat_completion",
    "post_chat_completion_sync",
]
