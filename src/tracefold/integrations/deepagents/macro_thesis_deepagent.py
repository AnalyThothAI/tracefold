from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from typing import TYPE_CHECKING, Any, Literal, Protocol

import deepagents
from deepagents import (
    GeneralPurposeSubagentProfile,
    HarnessProfile,
    create_deep_agent,
    register_harness_profile,
)
from langchain.agents.middleware import TodoListMiddleware
from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, SystemMessage
from langgraph.checkpoint.base import BaseCheckpointSaver
from pydantic import BaseModel, ConfigDict, Field, model_validator

from tracefold.macro import (
    MACRO_MODULE_IDS,
    MACRO_THESIS_ASSETS,
    MacroAlternative,
    MacroAssetOutlook,
    MacroCausalEdge,
    MacroChangeFromPrior,
    MacroCondition,
    MacroEvidencePackV3,
    MacroHorizonOutlook,
    MacroMainline,
    MacroModuleId,
    MacroModuleRole,
    MacroNarrativeSection,
    MacroTension,
    MacroTensionSide,
    MacroThesisBodyDraft,
    MacroThesisClaim,
    MacroThesisReviewV1,
    payload_hash,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable

    from langchain.agents.middleware.types import (
        ExtendedModelResponse,
        ModelRequest,
        ModelResponse,
        ResponseT,
    )

MACRO_THESIS_RESEARCH_PROMPT_VERSION = "macro_thesis_research_v1"
MACRO_THESIS_REVIEW_PROMPT_VERSION = "macro_thesis_independent_review_v1"
_IRRELEVANT_DEEPAGENT_TOOLS = frozenset(
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

_RESEARCH_PROMPT = """你是 Tracefold 的宏观主线研究 Agent。你只能使用当前冻结 Evidence Pack，
并产出一个前瞻性的 Macro Thesis，不得把确定性动量计算改写成模型事实。

硬约束：
- 这是无文件、无子图的单研究图；冻结 Evidence Pack 决策视图已完整放在用户消息中；
- 不调用任何检索、文件、todo 或 task 工具；只返回一个符合 required_output_schema 的 JSON 对象；
- 恰好一个 mainline，stance 只能是 call 或 no_call；
- alternative_explanation 最多一个，core_tensions 最多三个；
- 六个宏观模块必须各出现一次，角色只能是 driver、confirming、contradicting、uncertain；
- 资产顺序必须是 SPY, QQQ, IWM, TLT, IEF, LQD, HYG, UUP, GLD, USO, BTC, VIX；
- 每个资产必须分别写完整 outlook_1w 和 outlook_1m，不得重算 momentum_1w/momentum_1m；
- mainline 必须说明 stage、horizon、支持证据、冲突证据和因果边；
- alternative_explanation 最多一个；core_tensions 最多三个，每个必须写双方、领先方、
  滞后信号、未解决原因和可机械求值的 resolution trigger；
- 六个 module_assessments、changes_from_prior、资产 outlook 和 narrative_sections
  都必须引用冻结输入列出的 evidence_ref；
- 条件、引用和叙事章节由应用层根据你选择的模块和冻结 top_changes 机械装配；
- DeltaPack 为 bootstrap 时 changes_from_prior 必须为空；否则每项状态只能是
  new、strengthened、weakened、reversed、unchanged；
- 非 no_call 的 1W/1M outlook 必须同时包含 causal channel、支持证据、冲突证据、
  confirmation trigger、falsifier、checkpoint 和 confidence；
- 因果解释、条件展望和矛盾由你判断；覆盖、当前健康、历史深度、变化数值与来源身份不得改写。
"""

_REVIEW_PROMPT = """你是与宏观研究 Agent 分离的独立发布 Reviewer。你只审查指定 draft_hash 的草稿，
依据同一冻结 Evidence Pack 检查：
1. 引用是否闭合且真正支持声明；
2. 单主线、最多一个 alternative_explanation、最多三个 core_tensions、
   六个 module_assessments 和十二资产是否满足；
3. 因果链是否把相关性冒充因果；
4. falsifier/checkpoint 是否可被代码按 dataset/metric/operator/threshold 求值；
5. 是否遗漏当前数据中的主要反证或变化。

只能返回 pass、revise 或 block。revise/block 必须给出可执行 required_changes。
条件、引用与叙事章节由应用层从 Agent 选择的模块和冻结 top_changes 确定性装配；
只检查其引用闭合、指标存在和逻辑方向，不要求 no_call 资产拥有价格条件。
当 mainline=no_call 时，资产 no_call 是一致的保守结论，不应因缺少方向性条件而要求修订。
这是无文件状态工作流；冻结 Evidence Pack 决策视图已完整放在用户消息中。
不调用任何检索、文件、todo 或 task 工具。
只返回一个符合 required_output_schema 的 JSON 对象，不要使用 Markdown 围栏。
不要代替研究 Agent 重写草稿，也不要在结果中伪造 draft_hash、模型名或 invocation id；
这些绑定字段由应用层填写。
"""


class DeepAgentGraph(Protocol):
    async def ainvoke(
        self,
        input: Mapping[str, Any] | None,
        *,
        config: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]: ...


def _tool_name(value: Any) -> str | None:
    name = value.get("name") if isinstance(value, dict) else getattr(value, "name", None)
    return name if isinstance(name, str) else None


class _MacroThesisRequestBoundary(AgentMiddleware[Any, Any, Any]):
    """Replace generic DeepAgents instructions with the sealed one-shot contract."""

    def __init__(self, system_prompt: str) -> None:
        self._system_message = SystemMessage(content=system_prompt)

    def wrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], ModelResponse[Any]],
    ) -> ModelResponse[Any]:
        return handler(self._bounded_request(request))

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[
            [ModelRequest[Any]],
            Awaitable[ModelResponse[ResponseT]],
        ],
    ) -> ModelResponse[ResponseT] | AIMessage | ExtendedModelResponse[ResponseT]:
        return await handler(self._bounded_request(request))

    def _bounded_request(self, request: ModelRequest[Any]) -> ModelRequest[Any]:
        return request.override(
            system_message=self._system_message,
            tools=[value for value in request.tools if _tool_name(value) not in _IRRELEVANT_DEEPAGENT_TOOLS],
        )


class _AnalysisMainline(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    stance: Literal["call", "no_call"]
    title: str = Field(min_length=1, max_length=300)
    thesis: str = Field(min_length=1, max_length=4_000)
    stage: Literal["emerging", "developing", "mature", "reversing", "uncertain"]
    confidence: Literal["low", "medium", "high"]
    horizon: Literal["1w", "1m", "1w_to_1m"]
    claim: str = Field(min_length=1, max_length=2_000)
    causal_source: str = Field(min_length=1, max_length=300)
    causal_mechanism: str = Field(min_length=1, max_length=2_000)
    causal_target: str = Field(min_length=1, max_length=300)
    supporting_modules: tuple[MacroModuleId, ...] = Field(min_length=1, max_length=6)
    conflicting_modules: tuple[MacroModuleId, ...] = Field(default=(), max_length=6)


class _AnalysisAlternative(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    title: str = Field(min_length=1, max_length=300)
    thesis: str = Field(min_length=1, max_length=3_000)
    causal_source: str = Field(min_length=1, max_length=300)
    causal_mechanism: str = Field(min_length=1, max_length=2_000)
    causal_target: str = Field(min_length=1, max_length=300)
    supporting_modules: tuple[MacroModuleId, ...] = Field(min_length=1, max_length=6)
    conflicting_modules: tuple[MacroModuleId, ...] = Field(default=(), max_length=6)
    trigger_module: MacroModuleId


class _AnalysisTension(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    statement: str = Field(min_length=1, max_length=2_000)
    side_a_label: str = Field(min_length=1, max_length=300)
    side_a_statement: str = Field(min_length=1, max_length=1_500)
    side_a_module: MacroModuleId
    side_b_label: str = Field(min_length=1, max_length=300)
    side_b_statement: str = Field(min_length=1, max_length=1_500)
    side_b_module: MacroModuleId
    leading_side: Literal["side_a", "side_b", "balanced", "uncertain"]
    lagging_signal: str = Field(min_length=1, max_length=1_000)
    unresolved_reason: str = Field(min_length=1, max_length=1_500)
    resolution_module: MacroModuleId


class _AnalysisModule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    module_id: MacroModuleId
    role: Literal["driver", "confirming", "contradicting", "uncertain"]
    analysis: str = Field(min_length=1, max_length=3_000)


class _AnalysisChange(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["new", "strengthened", "weakened", "reversed", "unchanged"]
    statement: str = Field(min_length=1, max_length=2_000)
    modules: tuple[MacroModuleId, ...] = Field(min_length=1, max_length=6)


class _AnalysisAsset(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str
    direction_1w: Literal["bullish", "bearish", "neutral", "no_call"]
    channel_1w: str = Field(min_length=1, max_length=1_500)
    confidence_1w: Literal["low", "medium", "high"]
    direction_1m: Literal["bullish", "bearish", "neutral", "no_call"]
    channel_1m: str = Field(min_length=1, max_length=1_500)
    confidence_1m: Literal["low", "medium", "high"]
    supporting_modules: tuple[MacroModuleId, ...] = Field(min_length=1, max_length=6)
    conflicting_modules: tuple[MacroModuleId, ...] = Field(default=(), max_length=6)


class _AnalysisDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mainline: _AnalysisMainline
    alternative_explanation: _AnalysisAlternative | None = None
    core_tensions: tuple[_AnalysisTension, ...] = Field(default=(), max_length=3)
    module_assessments: tuple[_AnalysisModule, ...]
    changes_from_prior: tuple[_AnalysisChange, ...] = Field(default=(), max_length=6)
    asset_outlooks: tuple[_AnalysisAsset, ...]

    @model_validator(mode="after")
    def validate_order(self) -> _AnalysisDraft:
        if tuple(item.module_id for item in self.module_assessments) != MACRO_MODULE_IDS:
            raise ValueError("macro_thesis_analysis_module_order")
        if tuple(item.symbol for item in self.asset_outlooks) != MACRO_THESIS_ASSETS:
            raise ValueError("macro_thesis_analysis_asset_order")
        return self


def register_macro_thesis_harness_profile(
    *,
    model: BaseChatModel,
    model_name: str,
) -> str:
    """Bind the fileless single-graph contract to this exact resolved model."""

    provider = None
    try:
        params = model._get_ls_params()
    except (AttributeError, TypeError, NotImplementedError):
        params = None
    if isinstance(params, Mapping):
        value = params.get("ls_provider")
        provider = value.strip() if isinstance(value, str) else None
    if not provider:
        raise ValueError("macro_thesis_harness_provider_required")
    profile_key = f"{provider}:{require_supported_macro_thesis_model(model_name)}"
    register_harness_profile(
        profile_key,
        HarnessProfile(
            excluded_tools=_IRRELEVANT_DEEPAGENT_TOOLS,
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


class _ReviewDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    disposition: Literal["pass", "revise", "block"]
    findings: tuple[str, ...] = Field(default=(), max_length=20)
    required_changes: tuple[str, ...] = Field(default=(), max_length=20)

    @model_validator(mode="after")
    def validate_required_changes(self) -> _ReviewDraft:
        if self.disposition in {"revise", "block"} and not self.required_changes:
            raise ValueError("macro_thesis_review_required_changes_missing")
        return self


class MacroThesisDeepAgent:
    def __init__(
        self,
        *,
        model: BaseChatModel,
        model_name: str,
        checkpointer_context_factory: Callable[
            [],
            AbstractAsyncContextManager[BaseCheckpointSaver[Any]],
        ],
        graph_recursion_limit: int = 48,
        agent_factory: Callable[..., DeepAgentGraph] = create_deep_agent,
    ) -> None:
        self._model = model
        self._model_name = require_supported_macro_thesis_model(model_name)
        self._checkpointer_context_factory = checkpointer_context_factory
        self._graph_recursion_limit = _require_graph_recursion_limit(graph_recursion_limit)
        self._agent_factory = agent_factory
        self._harness_profile_key = register_macro_thesis_harness_profile(
            model=model,
            model_name=self._model_name,
        )

    async def draft(
        self,
        *,
        evidence_pack: MacroEvidencePackV3,
        revision_feedback: tuple[str, ...] = (),
        prior_draft: MacroThesisBodyDraft | None = None,
    ) -> tuple[MacroThesisBodyDraft, dict[str, Any]]:
        draft_seed = (
            {
                "evidence_pack_id": evidence_pack.evidence_pack_id,
                "revision": False,
            }
            if prior_draft is None
            else {
                "evidence_pack_id": evidence_pack.evidence_pack_id,
                "revision": True,
                "prior_draft_hash": payload_hash(prior_draft.model_dump(mode="json")),
                "revision_feedback": revision_feedback,
            }
        )
        invocation_id = (
            "research:" + evidence_pack.evidence_pack_id + ":" + payload_hash(draft_seed).removeprefix("sha256:")[:16]
        )
        async with self._checkpointer_context_factory() as checkpointer:
            graph = self._agent_factory(
                model=self._model,
                tools=(),
                system_prompt=_RESEARCH_PROMPT,
                subagents=(),
                middleware=(_MacroThesisRequestBoundary(_RESEARCH_PROMPT),),
                checkpointer=checkpointer,
                name="macro-thesis-research",
            )
            revision_context = (
                {
                    "prior_draft": prior_draft.model_dump(mode="json"),
                    "review_required_changes": revision_feedback,
                    "instruction": "这是唯一一次修订机会；逐项修复后返回完整新草稿。",
                }
                if prior_draft is not None
                else None
            )
            result = await graph.ainvoke(
                {
                    "messages": [
                        {
                            "role": "user",
                            "content": _json(
                                {
                                    "task": "基于冻结 pack 形成单一前瞻 Macro Thesis",
                                    "evidence_pack_id": evidence_pack.evidence_pack_id,
                                    "session_date": evidence_pack.session_date.isoformat(),
                                    "cutoff_ms": evidence_pack.cutoff_ms,
                                    "revision": revision_context,
                                    "required_output_schema": _AnalysisDraft.model_json_schema(),
                                    "sealed_evidence": _research_context(evidence_pack),
                                }
                            ),
                        }
                    ]
                },
                config=_graph_config(
                    invocation_id,
                    recursion_limit=self._graph_recursion_limit,
                ),
            )
        analysis = _response_model(result, _AnalysisDraft)
        draft = _compile_analysis_draft(
            analysis=analysis,
            evidence_pack=evidence_pack,
        )
        return draft, {
            "invocation_id": invocation_id,
            "model_name": self._model_name,
            "prompt_version": MACRO_THESIS_RESEARCH_PROMPT_VERSION,
            "deepagents_version": str(deepagents.__version__),
            "model_calls": _model_calls(result.get("messages", ())),
            "revision": prior_draft is not None,
            "harness_profile_key": self._harness_profile_key,
        }


class MacroThesisIndependentReviewer:
    def __init__(
        self,
        *,
        model: BaseChatModel,
        model_name: str,
        checkpointer_context_factory: Callable[
            [],
            AbstractAsyncContextManager[BaseCheckpointSaver[Any]],
        ],
        graph_recursion_limit: int = 48,
        agent_factory: Callable[..., DeepAgentGraph] = create_deep_agent,
    ) -> None:
        self._model = model
        self._model_name = require_supported_macro_thesis_model(model_name)
        self._checkpointer_context_factory = checkpointer_context_factory
        self._graph_recursion_limit = _require_graph_recursion_limit(graph_recursion_limit)
        self._agent_factory = agent_factory
        self._harness_profile_key = register_macro_thesis_harness_profile(
            model=model,
            model_name=self._model_name,
        )

    async def review(
        self,
        *,
        evidence_pack: MacroEvidencePackV3,
        draft: MacroThesisBodyDraft,
        draft_hash: str,
    ) -> MacroThesisReviewV1:
        invocation_id = "review:" + evidence_pack.evidence_pack_id + ":" + draft_hash.removeprefix("sha256:")[:16]
        async with self._checkpointer_context_factory() as checkpointer:
            graph = self._agent_factory(
                model=self._model,
                tools=(),
                system_prompt=_REVIEW_PROMPT,
                subagents=(),
                middleware=(_MacroThesisRequestBoundary(_REVIEW_PROMPT),),
                checkpointer=checkpointer,
                name="macro-thesis-independent-reviewer",
            )
            result = await graph.ainvoke(
                {
                    "messages": [
                        {
                            "role": "user",
                            "content": _json(
                                {
                                    "task": "独立审查此 draft_hash 绑定的完整草稿",
                                    "draft_hash": draft_hash,
                                    "draft": draft.model_dump(mode="json"),
                                    "evidence_pack_id": evidence_pack.evidence_pack_id,
                                    "required_output_schema": _ReviewDraft.model_json_schema(),
                                    "sealed_evidence": _research_context(evidence_pack),
                                }
                            ),
                        }
                    ]
                },
                config=_graph_config(
                    invocation_id,
                    recursion_limit=self._graph_recursion_limit,
                ),
            )
        review = _response_model(result, _ReviewDraft)
        return MacroThesisReviewV1(
            draft_hash=draft_hash,
            disposition=review.disposition,
            findings=review.findings,
            required_changes=review.required_changes,
            invocation_id=invocation_id,
            model_name=self._model_name,
            prompt_version=MACRO_THESIS_REVIEW_PROMPT_VERSION,
        )


def require_supported_macro_thesis_model(model_name: str) -> str:
    normalized = str(model_name or "").strip()
    if not normalized:
        raise ValueError("macro_thesis_model_required")
    leaf = normalized.rsplit("/", maxsplit=1)[-1].lower()
    if leaf.endswith(("-terra", "-sol")) or "codex" in leaf:
        raise ValueError("macro_thesis_unsupported_model:" + normalized)
    return normalized


def _require_graph_recursion_limit(value: int) -> int:
    normalized = int(value)
    if not 8 <= normalized <= 128:
        raise ValueError("macro_thesis_graph_recursion_limit_invalid")
    return normalized


def _graph_config(
    invocation_id: str,
    *,
    recursion_limit: int,
) -> dict[str, Any]:
    return {
        "configurable": {"thread_id": invocation_id},
        "recursion_limit": _require_graph_recursion_limit(recursion_limit),
    }


def _research_context(evidence_pack: MacroEvidencePackV3) -> dict[str, Any]:
    return {
        "evidence_pack_id": evidence_pack.evidence_pack_id,
        "schema_version": evidence_pack.schema_version,
        "session_date": evidence_pack.session_date.isoformat(),
        "cutoff_ms": evidence_pack.cutoff_ms,
        "prior_publication": evidence_pack.prior_publication,
        "delta_pack": evidence_pack.delta_pack,
        "catalyst_pack": evidence_pack.catalyst_pack,
        "momentum": [item.model_dump(mode="json") for item in evidence_pack.momentum],
        "modules": [
            _module_research_view(
                module=module,
                evidence_ref=(f"macro-module:{evidence_pack.session_date.isoformat()}:{module['module_id']}"),
            )
            for module in evidence_pack.modules
        ],
        "allowed_evidence_refs": [
            f"macro-module:{evidence_pack.session_date.isoformat()}:{module_id}" for module_id in MACRO_MODULE_IDS
        ],
    }


def _module_research_view(
    *,
    module: Mapping[str, Any],
    evidence_ref: str,
) -> dict[str, Any]:
    """Return decision-useful module evidence without raw historical arrays."""

    decision_view = {
        key: _without_historical_arrays(module[key])
        for key in (
            "schema_version",
            "module_id",
            "label",
            "latest_fact_at_ms",
            "status",
            "summary",
            "contradictions",
            "falsifiers",
            "next_checkpoints",
        )
        if key in module
    }
    return {
        "evidence_ref": evidence_ref,
        "module": decision_view,
    }


def _compile_analysis_draft(
    *,
    analysis: _AnalysisDraft,
    evidence_pack: MacroEvidencePackV3,
) -> MacroThesisBodyDraft:
    main = analysis.mainline
    supporting_refs = _module_refs(evidence_pack, main.supporting_modules)
    conflicting_refs = _module_refs(evidence_pack, main.conflicting_modules)
    signal_module = _first_signal_module(evidence_pack, main.supporting_modules)
    claim_id = "mainline-claim-1"
    mainline = MacroMainline(
        stance=main.stance,
        title=main.title,
        thesis=main.thesis,
        stage=main.stage,
        confidence=main.confidence,
        horizon=main.horizon,
        claims=(
            MacroThesisClaim(
                claim_id=claim_id,
                statement=main.claim,
                causal_edges=(
                    MacroCausalEdge(
                        source=main.causal_source,
                        mechanism=main.causal_mechanism,
                        target=main.causal_target,
                        evidence_refs=supporting_refs,
                        conflicting_evidence_refs=conflicting_refs,
                    ),
                ),
                supporting_evidence_refs=supporting_refs,
                conflicting_evidence_refs=conflicting_refs,
                conditions=(
                    _pack_condition(
                        evidence_pack,
                        module_id=signal_module,
                        condition_id="mainline-confirm",
                        kind="confirm",
                        rationale="主线驱动模块的首要变化继续满足冻结方向。",
                    ),
                ),
            ),
        ),
        supporting_evidence_refs=supporting_refs,
        conflicting_evidence_refs=conflicting_refs,
        falsifiers=(
            _pack_condition(
                evidence_pack,
                module_id=signal_module,
                condition_id="mainline-falsifier",
                kind="falsifier",
                rationale="主线驱动模块的首要变化反向穿越零轴。",
            ),
        ),
        checkpoints=(
            _pack_condition(
                evidence_pack,
                module_id=signal_module,
                condition_id="mainline-checkpoint",
                kind="confirm",
                rationale="下次检查主线驱动模块是否延续。",
            ),
        ),
    )
    alternative = _compile_alternative(
        analysis.alternative_explanation,
        evidence_pack=evidence_pack,
    )
    tensions = tuple(
        _compile_tension(item, index=index, evidence_pack=evidence_pack)
        for index, item in enumerate(analysis.core_tensions, start=1)
    )
    module_assessments = tuple(
        MacroModuleRole(
            module_id=item.module_id,
            role=item.role,
            analysis=item.analysis,
            claim_ids=(claim_id,),
            supporting_evidence_refs=_module_refs(evidence_pack, (item.module_id,)),
        )
        for item in analysis.module_assessments
    )
    changes = (
        tuple(
            MacroChangeFromPrior(
                change_id=f"prior-change-{index}",
                status=item.status,
                statement=item.statement,
                evidence_refs=_module_refs(evidence_pack, item.modules),
            )
            for index, item in enumerate(analysis.changes_from_prior, start=1)
        )
        if evidence_pack.prior_publication is not None
        else ()
    )
    assets = tuple(
        _compile_asset(
            item,
            evidence_pack=evidence_pack,
            force_no_call=main.stance == "no_call",
        )
        for item in analysis.asset_outlooks
    )
    tension_markdown = "\n".join(
        f"- {item.statement}（领先方：{item.leading_side}；待观察：{item.lagging_signal}）" for item in tensions
    )
    narrative_sections = (
        MacroNarrativeSection(
            section_id="market-mainline",
            title="市场主线与短期矛盾",
            markdown=(
                f"{mainline.thesis}\n\n"
                + (f"替代解释：{alternative.thesis}\n\n" if alternative is not None else "")
                + (f"核心矛盾：\n{tension_markdown}" if tension_markdown else "当前没有额外核心矛盾。")
            ),
            evidence_refs=tuple(
                dict.fromkeys(
                    (
                        *supporting_refs,
                        *conflicting_refs,
                        *(alternative.supporting_evidence_refs if alternative is not None else ()),
                    )
                )
            ),
        ),
        *(
            MacroNarrativeSection(
                section_id=f"module-{item.module_id}",
                title=f"{item.module_id} 模块判断",
                markdown=item.analysis,
                evidence_refs=_module_refs(evidence_pack, (item.module_id,)),
            )
            for item in analysis.module_assessments
        ),
    )
    return MacroThesisBodyDraft(
        mainline=mainline,
        alternative_explanation=alternative,
        core_tensions=tensions,
        module_assessments=module_assessments,
        changes_from_prior=changes,
        asset_outlooks=assets,
        narrative_sections=narrative_sections,
    )


def _compile_alternative(
    item: _AnalysisAlternative | None,
    *,
    evidence_pack: MacroEvidencePackV3,
) -> MacroAlternative | None:
    if item is None:
        return None
    supporting_refs = _module_refs(evidence_pack, item.supporting_modules)
    conflicting_refs = _module_refs(evidence_pack, item.conflicting_modules)
    return MacroAlternative(
        title=item.title,
        thesis=item.thesis,
        causal_edges=(
            MacroCausalEdge(
                source=item.causal_source,
                mechanism=item.causal_mechanism,
                target=item.causal_target,
                evidence_refs=supporting_refs,
                conflicting_evidence_refs=conflicting_refs,
            ),
        ),
        supporting_evidence_refs=supporting_refs,
        conflicting_evidence_refs=conflicting_refs,
        trigger_conditions=(
            _pack_condition(
                evidence_pack,
                module_id=item.trigger_module,
                condition_id="alternative-trigger",
                kind="confirm",
                rationale="替代解释对应模块的首要变化达到冻结方向。",
            ),
        ),
    )


def _compile_tension(
    item: _AnalysisTension,
    *,
    index: int,
    evidence_pack: MacroEvidencePackV3,
) -> MacroTension:
    return MacroTension(
        tension_id=f"tension-{index}",
        statement=item.statement,
        side_a=MacroTensionSide(
            label=item.side_a_label,
            statement=item.side_a_statement,
            evidence_refs=_module_refs(evidence_pack, (item.side_a_module,)),
        ),
        side_b=MacroTensionSide(
            label=item.side_b_label,
            statement=item.side_b_statement,
            evidence_refs=_module_refs(evidence_pack, (item.side_b_module,)),
        ),
        leading_side=item.leading_side,
        lagging_signal=item.lagging_signal,
        unresolved_reason=item.unresolved_reason,
        resolution_triggers=(
            _pack_condition(
                evidence_pack,
                module_id=item.resolution_module,
                condition_id=f"tension-{index}-resolution",
                kind="confirm",
                rationale="矛盾解决模块的首要变化满足冻结方向。",
            ),
        ),
    )


def _compile_asset(
    item: _AnalysisAsset,
    *,
    evidence_pack: MacroEvidencePackV3,
    force_no_call: bool,
) -> MacroAssetOutlook:
    supporting_refs = _module_refs(evidence_pack, item.supporting_modules)
    conflicting_refs = _module_refs(evidence_pack, item.conflicting_modules)
    momentum = next(value for value in evidence_pack.momentum if value.symbol == item.symbol)

    def horizon(
        *,
        value: Literal["1w", "1m"],
        direction: Literal["bullish", "bearish", "neutral", "no_call"],
        channel: str,
        confidence: Literal["low", "medium", "high"],
    ) -> MacroHorizonOutlook:
        momentum_state = momentum.momentum_1w if value == "1w" else momentum.momentum_1m
        asset_signal = _asset_signal(evidence_pack, item.symbol, value)
        if force_no_call or direction == "no_call" or momentum_state == "insufficient" or asset_signal is None:
            return MacroHorizonOutlook(
                horizon=value,
                direction="no_call",
                causal_channel=channel + " 当前主线或资产自身可计算动量不足，方向降级为 no_call。",
                supporting_evidence_refs=supporting_refs,
                conflicting_evidence_refs=conflicting_refs,
                confidence="low",
            )
        prefix = f"asset-{item.symbol.lower()}-{value}"
        return MacroHorizonOutlook(
            horizon=value,
            direction=direction,
            causal_channel=channel,
            supporting_evidence_refs=supporting_refs,
            conflicting_evidence_refs=conflicting_refs,
            confirmation_triggers=(
                _pack_condition(
                    evidence_pack,
                    module_id="cross_asset",
                    condition_id=f"{prefix}-confirm",
                    kind="confirm",
                    rationale=f"{item.symbol} {value} 展望的驱动模块继续满足冻结方向。",
                    signal=asset_signal,
                ),
            ),
            falsifiers=(
                _pack_condition(
                    evidence_pack,
                    module_id="cross_asset",
                    condition_id=f"{prefix}-falsifier",
                    kind="falsifier",
                    rationale=f"{item.symbol} {value} 展望的驱动模块反向穿越零轴。",
                    signal=asset_signal,
                ),
            ),
            checkpoints=(
                _pack_condition(
                    evidence_pack,
                    module_id="cross_asset",
                    condition_id=f"{prefix}-checkpoint",
                    kind="confirm",
                    rationale=f"复核 {item.symbol} {value} 展望的驱动变化。",
                    signal=asset_signal,
                ),
            ),
            confidence=confidence,
        )

    return MacroAssetOutlook(
        symbol=item.symbol,
        outlook_1w=horizon(
            value="1w",
            direction=item.direction_1w,
            channel=item.channel_1w,
            confidence=item.confidence_1w,
        ),
        outlook_1m=horizon(
            value="1m",
            direction=item.direction_1m,
            channel=item.channel_1m,
            confidence=item.confidence_1m,
        ),
    )


def _module_refs(
    evidence_pack: MacroEvidencePackV3,
    module_ids: Sequence[MacroModuleId],
) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(f"macro-module:{evidence_pack.session_date.isoformat()}:{module_id}" for module_id in module_ids)
    )


def _first_signal_module(
    evidence_pack: MacroEvidencePackV3,
    preferred: Sequence[MacroModuleId],
) -> MacroModuleId:
    for module_id in (*preferred, *MACRO_MODULE_IDS):
        try:
            _primary_signal(evidence_pack, module_id)
        except ValueError:
            continue
        return module_id
    raise ValueError("macro_thesis_pack_has_no_condition_signal")


def _pack_condition(
    evidence_pack: MacroEvidencePackV3,
    *,
    module_id: MacroModuleId,
    condition_id: str,
    kind: Literal["confirm", "falsifier"],
    rationale: str,
    signal: tuple[str, str, float] | None = None,
) -> MacroCondition:
    dataset_id, metric_name, current = signal or _primary_signal(
        evidence_pack,
        module_id,
    )
    if kind == "confirm":
        operator = "gte" if current >= 0 else "lte"
        threshold = current
        effect = "confirming"
    else:
        operator = "lt" if current >= 0 else "gt"
        threshold = 0.0
        effect = "invalidation_triggered"
    return MacroCondition(
        condition_id=condition_id,
        module_id=module_id,
        dataset_id=dataset_id,
        metric_name=metric_name,
        operator=operator,
        threshold=threshold,
        effect=effect,
        rationale=rationale,
    )


def _asset_signal(
    evidence_pack: MacroEvidencePackV3,
    symbol: str,
    horizon: Literal["1w", "1m"],
) -> tuple[str, str, float] | None:
    momentum = next(value for value in evidence_pack.momentum if value.symbol == symbol)
    dataset_id = momentum.source_dataset_id
    metric_name = f"return_{horizon}_pct"
    if not dataset_id:
        return None
    module = next(value for value in evidence_pack.modules if value.get("module_id") == "cross_asset")
    for change in dict(module.get("summary") or {}).get("top_changes") or ():
        if not isinstance(change, Mapping) or change.get("dataset_id") != dataset_id:
            continue
        metrics = change.get("metrics")
        metric_value = metrics.get(metric_name) if isinstance(metrics, Mapping) else None
        if isinstance(metric_value, (int, float)) and not isinstance(metric_value, bool):
            return dataset_id, metric_name, float(metric_value)
    return None


def _primary_signal(
    evidence_pack: MacroEvidencePackV3,
    module_id: MacroModuleId,
) -> tuple[str, str, float]:
    module = next(
        (value for value in evidence_pack.modules if value.get("module_id") == module_id),
        None,
    )
    if module is None:
        raise ValueError("macro_thesis_module_missing:" + module_id)
    top_changes = dict(module.get("summary") or {}).get("top_changes") or ()
    for change in top_changes:
        if not isinstance(change, Mapping):
            continue
        dataset_id = str(change.get("dataset_id") or "")
        metrics = change.get("metrics")
        if not dataset_id or not isinstance(metrics, Mapping):
            continue
        numeric = [
            (str(name), float(value))
            for name, value in metrics.items()
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        ]
        if numeric:
            metric_name, current = max(numeric, key=lambda item: abs(item[1]))
            return dataset_id, metric_name, current
    raise ValueError("macro_thesis_module_condition_signal_missing:" + module_id)


def _without_historical_arrays(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _without_historical_arrays(item) for key, item in value.items() if str(key) != "history"}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_without_historical_arrays(item) for item in value]
    return value


def _response_model(result: Mapping[str, Any], expected: type[BaseModel]) -> Any:
    messages = result.get("messages")
    if not isinstance(messages, Sequence):
        raise RuntimeError("macro_thesis_response_messages_missing")
    for message in reversed(messages):
        if not isinstance(message, AIMessage):
            continue
        text = message.text.strip()
        if not text:
            continue
        fenced = re.fullmatch(
            r"```(?:json)?\s*(?P<body>\{.*\})\s*```",
            text,
            flags=re.DOTALL | re.IGNORECASE,
        )
        return expected.model_validate_json(fenced.group("body") if fenced is not None else text)
    raise RuntimeError("macro_thesis_response_text_missing")


def _model_calls(messages: object) -> int:
    if not isinstance(messages, Sequence):
        return 0
    return sum(isinstance(message, AIMessage) for message in messages)


def _json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


__all__ = [
    "MACRO_THESIS_RESEARCH_PROMPT_VERSION",
    "MACRO_THESIS_REVIEW_PROMPT_VERSION",
    "MacroThesisDeepAgent",
    "MacroThesisIndependentReviewer",
    "register_macro_thesis_harness_profile",
    "require_supported_macro_thesis_model",
]
