"""An ordered, scripted DSPy LM for tests that drive a real DSPy module without a provider.

It sits below the public DSPy LM entry as a canonical engine, exactly where a provider would answer: each
physical request takes the next step, which is a JSON-able mapping, a raw string, a `Response`, an exception
to raise, or a callable of the request returning one of those. Every request is recorded on `requests`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator, Sequence
from typing import Any, Literal

import dspy  # type: ignore[import-untyped]
from dspy.lm15 import Message, Request, Response, Usage, response_to_events

from tracefold.news.artifact_identity import canonical_json


class _SyncEngine:
    def __init__(self, owner: ScriptedLM) -> None:
        self.owner = owner

    def complete(self, request: Request) -> Response:
        return self.owner._next(request)

    def stream(self, request: Request) -> Iterator[Any]:
        return iter(response_to_events(self.complete(request)))

    def close(self) -> None:
        return None


class _AsyncEngine:
    def __init__(self, owner: ScriptedLM) -> None:
        self.owner = owner

    async def complete(self, request: Request) -> Response:
        return self.owner._next(request)

    async def stream(self, request: Request) -> AsyncIterator[Any]:
        for event in response_to_events(self.owner._next(request)):
            yield event

    async def aclose(self) -> None:
        return None


class ScriptedLM(dspy.LM):  # type: ignore[misc]
    def __init__(
        self,
        steps: Sequence[Any],
        *,
        model: str = "scripted/test",
        structured_output: Literal["json_schema", "json_object", "prompt_json"] = "json_schema",
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
            engine=_SyncEngine(self),
            async_engine=_AsyncEngine(self),
            **kwargs,
        )

    def copy(self, **kwargs: Any) -> ScriptedLM:
        return ScriptedLM(
            list(self._steps), model=self.model, structured_output=self._structured_output, **{**self.kwargs, **kwargs}
        )

    @property
    def supported_params(self) -> set[str]:
        return set() if self._structured_output == "prompt_json" else {"response_format"}

    @property
    def supports_response_schema(self) -> bool:
        return self._structured_output == "json_schema"

    def _next(self, request: Request) -> Response:
        self.requests.append(request)
        if not self._steps:
            raise dspy.LMUnexpectedError("scripted_lm_script_exhausted")
        step = self._steps.pop(0)
        if callable(step):
            step = step(request)
        if isinstance(step, BaseException):
            raise step
        if isinstance(step, Response):
            return step
        text = step if isinstance(step, str) else canonical_json(step)
        return Response(
            id=None,
            model=self.model,
            message=Message.assistant(text),
            finish_reason="stop",
            usage=Usage(input_tokens=0, output_tokens=0, total_tokens=0, cache_read_tokens=0),
        )
