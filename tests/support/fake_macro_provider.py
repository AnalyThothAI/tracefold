from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any, Self

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, BaseMessage
from pydantic import Field


class RecordingStructuredMacroModel(GenericFakeChatModel):
    """Fake only the outer provider transport while retaining the real agent graph."""

    model_name: str = "macro-full-stack-fake"
    invocation_count: int = 0
    bound_tool_names: tuple[str, ...] = ()
    response_format: dict[str, Any] | None = None
    request_messages: tuple[BaseMessage, ...] = ()
    request_metadata: dict[str, Any] = Field(default_factory=dict)
    request_metadata_history: tuple[dict[str, Any], ...] = ()

    @classmethod
    def for_mapping(
        cls,
        mapping: dict[str, Any],
        *,
        response_id: str = "macro-full-stack-response-1",
    ) -> Self:
        return cls(
            messages=iter(
                [
                    AIMessage(
                        content=json.dumps(mapping, ensure_ascii=False),
                        id=response_id,
                    )
                ]
            ),
            profile={"structured_output": True},
        )

    def _get_ls_params(self, **_kwargs: Any) -> dict[str, str]:
        return {
            "ls_provider": "tracefold-fake-provider",
            "ls_model_name": self.model_name,
        }

    def bind_tools(
        self,
        tools: Sequence[Any],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> Any:
        self.bound_tool_names = tuple(
            str(getattr(tool, "name", None) or tool.get("name"))
            if isinstance(tool, dict)
            else str(getattr(tool, "name", type(tool).__name__))
            for tool in tools
        )
        response_format = kwargs.get("response_format")
        if isinstance(response_format, dict):
            self.response_format = response_format
        return self.bind(**kwargs)

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> Any:
        self._record_request(messages, run_manager)
        return await super()._agenerate(
            messages,
            stop=stop,
            run_manager=run_manager,
            **kwargs,
        )

    def _record_request(self, messages: list[BaseMessage], run_manager: Any) -> None:
        self.invocation_count += 1
        self.request_messages = tuple(messages)
        metadata = getattr(run_manager, "metadata", None)
        self.request_metadata = dict(metadata) if isinstance(metadata, dict) else {}
        self.request_metadata_history = (*self.request_metadata_history, self.request_metadata)


class TransientRecordingStructuredMacroModel(RecordingStructuredMacroModel):
    """Fail the first provider call, then return the configured native response."""

    failures_remaining: int = 1

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> Any:
        if self.failures_remaining > 0:
            self._record_request(messages, run_manager)
            self.failures_remaining -= 1
            raise TimeoutError("transient macro provider timeout")
        return await super()._agenerate(
            messages,
            stop=stop,
            run_manager=run_manager,
            **kwargs,
        )
