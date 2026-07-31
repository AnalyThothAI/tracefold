from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any, Protocol

from deepagents import (
    GeneralPurposeSubagentProfile,
    HarnessProfile,
    create_deep_agent,
    register_harness_profile,
)
from langchain.agents.middleware import TodoListMiddleware
from langchain.agents.middleware.types import AgentMiddleware
from langchain.agents.structured_output import ProviderStrategy
from langchain_core.language_models import BaseChatModel

from tracefold.macro import (
    MACRO_THESIS_PROFILE_VERSION,
    MACRO_THESIS_PROMPT_VERSION,
    CandidateDraftEnvelope,
    MacroModelExpectedError,
    MacroResearchInputV1,
    MacroThesisDraftV2,
    canonical_json_bytes,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable

    from langchain.agents.middleware.types import ModelRequest, ModelResponse, ResponseT

_THIN_EXCLUDED_TOOLS = frozenset(
    {
        "edit_file",
        "execute",
        "glob",
        "grep",
        "ls",
        "read_file",
        "task",
        "write_file",
        "write_todos",
    }
)

_RESEARCH_PROMPT = """你是 Tracefold 的 Thin Macro Research Agent。

只分析用户消息中的 cutoff-sealed MacroResearchInputV1，按以下 SOP 返回原生 structured draft：
1. 盘点 exact evidence、局部 gap 与 counter-signal。
2. 选择最强 driver；有方向时用 1–3 条 driver → mechanism → outcome 因果边形成唯一 mainline，
   证据冲突或不足时明确 no_call。
3. 主动写出 strongest counterevidence；只有确有不同因果解释时才提供最多一个 alternative，
   tensions 最多三个。
4. material_changes 是必答选择：只能选择 modules.material_changes 中已有的 candidate_id，
   填 status 与 statement；没有实质 Thesis 变化时明确返回空数组，不得用普通事实变化凑数。
5. 只选择有 exact evidence 和可解释 transmission 的 material module 与 material asset outlook；
   不填满六模块或十二资产。
6. 条件只能选择输入给出的 candidate_id、allowed kind 和 allowed scope；不得改写 predicate。
   stance=call 时必须选择至少一个 scope_kind=mainline、scope_id=mainline 的 falsifier；
   falsifier 在 cutoff 时必须尚未触发；没有合格 falsifier 时必须 no_call。
7. 每个事实性判断完成 exact evidence_ref closure，然后只返回 MacroThesisDraftV2 structured output。

不得选择 canonical source、重算 deterministic facts、发明 Dataset/metric/operator/threshold，
不得调用工具、子 Agent、文件、todo、task、execute、search 或 summarization。
"""


class DeepAgentGraph(Protocol):
    async def ainvoke(
        self,
        input: Mapping[str, Any] | None,
        *,
        config: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]: ...


class _SingleModelInvocationBoundary(AgentMiddleware[Any, Any, Any]):
    """Fail closed if the framework attempts a second model call in one durable attempt."""

    def __init__(self, *, on_model_submitted: Callable[[], None]) -> None:
        self.calls = 0
        self._on_model_submitted = on_model_submitted

    def wrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], ModelResponse[Any]],
    ) -> ModelResponse[Any]:
        self._claim()
        return handler(request)

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[ResponseT]]],
    ) -> ModelResponse[ResponseT]:
        self._claim()
        return await handler(request)

    def _claim(self) -> None:
        self.calls += 1
        if self.calls != 1:
            raise RuntimeError("macro_thesis_attempt_multiple_model_calls")
        self._on_model_submitted()


def register_macro_thesis_harness_profile(
    *,
    model: BaseChatModel,
    model_name: str,
) -> str:
    provider = _provider_name(model)
    profile_key = f"{provider}:{require_supported_macro_thesis_model(model_name)}"
    register_harness_profile(
        profile_key,
        HarnessProfile(
            base_system_prompt="",
            excluded_tools=_THIN_EXCLUDED_TOOLS,
            excluded_middleware=frozenset(
                {
                    TodoListMiddleware,
                    "SummarizationMiddleware",
                }
            ),
            general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
        ),
    )
    return profile_key


class MacroThesisDeepAgent:
    """DeepAgents adapter for the one-graph, one-model-call Thin Profile."""

    def __init__(
        self,
        *,
        model: BaseChatModel,
        model_name: str,
        agent_factory: Callable[..., DeepAgentGraph] = create_deep_agent,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        self._model = model
        self._model_name = require_supported_macro_thesis_model(model_name)
        self._agent_factory = agent_factory
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1_000))
        self._provider_name = _provider_name(model)
        self._harness_profile_key = register_macro_thesis_harness_profile(
            model=model,
            model_name=self._model_name,
        )

    async def draft(
        self,
        *,
        research_input: MacroResearchInputV1,
        attempt_id: str,
        on_model_submitted: Callable[[], None],
    ) -> CandidateDraftEnvelope:
        normalized_attempt_id = str(attempt_id or "").strip()
        if not normalized_attempt_id:
            raise ValueError("macro_thesis_attempt_id_required")
        boundary = _SingleModelInvocationBoundary(on_model_submitted=on_model_submitted)
        graph = self._agent_factory(
            model=self._model,
            tools=(),
            system_prompt=_RESEARCH_PROMPT,
            subagents=(),
            middleware=(boundary,),
            response_format=ProviderStrategy(MacroThesisDraftV2.model_json_schema()),
            checkpointer=None,
            name="macro-thesis-thin",
        )
        try:
            result = await graph.ainvoke(
                {
                    "messages": [
                        {
                            "role": "user",
                            "content": canonical_json_bytes(research_input.model_dump(mode="json")).decode("utf-8"),
                        }
                    ]
                },
                config={
                    "metadata": {
                        "macro_attempt_id": normalized_attempt_id,
                        "macro_research_input_id": research_input.input_id,
                    },
                    "recursion_limit": 8,
                },
            )
        except Exception as exc:
            if _is_expected_model_failure(exc):
                raise MacroModelExpectedError(f"macro_thesis_model_expected:{type(exc).__name__}") from exc
            raise
        if boundary.calls != 1:
            raise RuntimeError("macro_thesis_attempt_model_call_count_invalid")
        structured = result.get("structured_response")
        if hasattr(structured, "model_dump"):
            structured = structured.model_dump(mode="json")
        if not isinstance(structured, Mapping):
            raise RuntimeError("macro_thesis_provider_structured_mapping_missing")
        response_id = _provider_response_id(result)
        return CandidateDraftEnvelope(
            attempt_id=normalized_attempt_id,
            provider_response_id=response_id,
            provider_name=self._provider_name,
            model_name=self._model_name,
            profile_version=MACRO_THESIS_PROFILE_VERSION,
            prompt_version=MACRO_THESIS_PROMPT_VERSION,
            research_input_id=research_input.input_id,
            research_input_hash=research_input.input_hash,
            raw_structured_mapping=dict(structured),
            received_at_ms=int(self._clock_ms()),
            model_calls=1,
        )

    @property
    def harness_profile_key(self) -> str:
        return self._harness_profile_key


def require_supported_macro_thesis_model(model_name: str) -> str:
    normalized = str(model_name or "").strip()
    if not normalized:
        raise ValueError("macro_thesis_model_required")
    leaf = normalized.rsplit("/", maxsplit=1)[-1].lower()
    if leaf.endswith(("-terra", "-sol")) or "codex" in leaf:
        raise ValueError("macro_thesis_unsupported_model:" + normalized)
    return normalized


def _is_expected_model_failure(exc: Exception) -> bool:
    message = str(exc).lower()
    module = type(exc).__module__.split(".", maxsplit=1)[0]
    name = type(exc).__name__.lower()
    if any(
        marker in message
        for marker in (
            "macro_thesis_attempt_multiple_model_calls",
            "macro_thesis_attempt_model_call_count_invalid",
            "macro_thesis_provider_structured_mapping_missing",
        )
    ):
        return True
    return module in {
        "httpx",
        "openai",
        "litellm",
        "pydantic",
        "pydantic_core",
    } or any(token in name for token in ("timeout", "ratelimit", "apierror", "connection"))


def _provider_name(model: BaseChatModel) -> str:
    try:
        params = model._get_ls_params()
    except (AttributeError, TypeError, NotImplementedError):
        params = None
    value = params.get("ls_provider") if isinstance(params, Mapping) else None
    provider = value.strip() if isinstance(value, str) else ""
    if not provider:
        raise ValueError("macro_thesis_harness_provider_required")
    return provider


def _provider_response_id(result: Mapping[str, Any]) -> str:
    direct = result.get("response_id")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    messages = result.get("messages")
    if isinstance(messages, list | tuple):
        for message in reversed(messages):
            identifier = getattr(message, "id", None)
            if isinstance(identifier, str) and identifier.strip():
                return identifier.strip()
            metadata = getattr(message, "response_metadata", None)
            if isinstance(metadata, Mapping):
                for key in ("id", "response_id", "request_id"):
                    value = metadata.get(key)
                    if isinstance(value, str) and value.strip():
                        return value.strip()
    raise RuntimeError("macro_thesis_provider_response_identity_missing")


__all__ = [
    "MACRO_THESIS_PROFILE_VERSION",
    "MACRO_THESIS_PROMPT_VERSION",
    "MacroThesisDeepAgent",
    "register_macro_thesis_harness_profile",
    "require_supported_macro_thesis_model",
]
