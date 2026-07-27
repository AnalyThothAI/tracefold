from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from datetime import date
from pathlib import Path
from typing import Any, Protocol

import deepagents
from deepagents import create_deep_agent
from deepagents.backends import CompositeBackend, LocalShellBackend, StateBackend
from langchain.agents.structured_output import ToolStrategy
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.tools import BaseTool, tool
from langgraph.checkpoint.base import BaseCheckpointSaver

from tracefold.macro import (
    MACRO_RESEARCH_MAX_PRIOR_PUBLICATIONS_PER_PAGE,
    MACRO_RESEARCH_MAX_READ_REFS,
    FrozenMacroEvidenceScope,
    MacroEvidenceQuery,
    MacroEvidenceRecord,
    MacroResearchAgentResult,
    MacroResearchArtifactDraft,
    MacroResearchAudit,
    MacroResearchIntegrityError,
    MacroResearchReadPort,
    canonicalize_macro_research_artifact,
    require_artifact_integrity,
    require_catalog_in_scope,
    require_evidence_in_scope,
    require_prior_research_in_scope,
)

MACRO_RESEARCH_PROMPT_VERSION = "macro_research_parent_v5"
MACRO_RESEARCH_WORKFLOW_VERSION = "deepagents_macro_research_v4_evidence_pack"
MACRO_RESEARCH_TOOL_NAMES = (
    "inspect_macro_evidence_catalog",
    "search_macro_evidence",
    "read_macro_evidence",
    "read_prior_macro_research",
)
_SEARCH_PAYLOAD_TARGET_CHARS = 80_000

_PARENT_SYSTEM_PROMPT = """你是 Tracefold 的宏观研究主 Agent。你的工作对象是一个已冻结的完成交易日证据范围，
最终产出一份连贯、专业、面向中文操作者的宏观研究文档。

你拥有并应自主使用 DeepAgents 的原生能力：
- 用 write_todos 建立并动态调整研究计划；
- 用虚拟文件系统保存假设、证据摘录、反证和草稿，避免把全部上下文堆在一次回复中；
- 按问题需要自由选择证据工具，不存在固定调用顺序；
- 通过 task 按需委派 evidence-analyst、cross-asset-challenger、skeptical-editor；
- 用 execute 做数值计算、表格处理或假设检验；需要脚本或中间文件时写入 `/workspace/`，
  execute 的当前目录就是该 workspace，因此命令中使用相对文件名；
- 自主判断还要查什么、哪些证据存在冲突、哪些 gap 应明确留下，以及何时完成。

所有市场事实只能来自当前 scope 绑定的只读工具。DeepAgents 原生文件系统和 execute 可用于计算、
整理、比较和草稿；计算产物不得冒充新的市场事实，结论仍须回到工具返回的 source_ref。
不得绕过 scope 直连实时网页、provider 或数据库，也不得构造工具未返回的 source_ref。
历史研究只提供上下文，不可替代 material fact 引用。

最终 MacroResearchArtifact 的 section、gap、标题和论证结构由你决定；不要套用固定六页、八类 risk lane、
固定方向、readiness 或 no_call 模板。定稿前必须委派 skeptical-editor 审阅，并根据意见修订；
只有完成修订且事实与引用闭合时 reviewer_disposition 才能为 pass，否则必须为 revise 或 block。
正文、摘要、section 与 reviewer notes 应使用自然、完整的中文，
但应用代码不会通过关键词或语言比例替你做语义判断。引用 citation_id 必须在 citations 中定义，
每个 citations[] 只提交 citation_id 与 source_ref；source_ref 必须逐字复制自证据工具返回值。
source type、label、时间、URL 与 lineage 由应用层从已披露记录机械补齐，不得自行填写。
"""

_SPECIALISTS = (
    {
        "name": "evidence-analyst",
        "description": "深入检索冻结事实与官方/高质量文本，整理机制、矛盾、引用和未解问题。",
        "system_prompt": (
            "你是隔离的宏观证据分析员。根据父 Agent 的具体任务自主查询冻结证据，"
            "返回紧凑的事实、机制、反证、source_ref 与 gap。不要代替父 Agent定稿。"
        ),
    },
    {
        "name": "cross-asset-challenger",
        "description": "从利率、信用、流动性、美元、商品、波动率和风险资产之间寻找冲突与替代解释。",
        "system_prompt": (
            "你是跨资产反证研究员。主动挑战父 Agent 给出的假设，查询当前 frozen scope，"
            "区分同步、领先、滞后与缺失证据，并返回可核验 source_ref。"
        ),
    },
    {
        "name": "skeptical-editor",
        "description": "审阅草稿的事实闭合、因果跳跃、遗漏反证和中文表达，并提出可执行修订。",
        "system_prompt": (
            "你是怀疑主义宏观编辑。审查父 Agent 提供的草稿与任务上下文，按需复查冻结证据，"
            "指出事实错配、未知引用、因果跳跃、遗漏反证和表达问题，并明确建议 pass、revise 或 block。"
        ),
    },
)


class DeepAgentGraph(Protocol):
    async def ainvoke(
        self,
        input: Mapping[str, Any] | None,
        *,
        config: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]: ...


class MacroResearchDeepAgent:
    def __init__(
        self,
        *,
        model: BaseChatModel,
        model_name: str,
        reader: MacroResearchReadPort,
        checkpointer_context_factory: Callable[
            [],
            AbstractAsyncContextManager[BaseCheckpointSaver[Any]],
        ],
        workspace_root: Path | None = None,
        agent_factory: Callable[..., DeepAgentGraph] = create_deep_agent,
    ) -> None:
        self._model = model
        self._model_name = str(model_name).strip()
        if not self._model_name:
            raise ValueError("macro_research_model_name_required")
        self._reader = reader
        self._checkpointer_context_factory = checkpointer_context_factory
        self._workspace_root = None if workspace_root is None else Path(workspace_root)
        self._agent_factory = agent_factory

    async def analyze(
        self,
        scope: FrozenMacroEvidenceScope,
    ) -> MacroResearchAgentResult:
        tool_session = _EvidenceToolSession(scope=scope, reader=self._reader)
        evidence_tools = tool_session.tools()
        subagents = tuple(
            {
                **specialist,
                "model": self._model,
                "tools": evidence_tools,
            }
            for specialist in _SPECIALISTS
        )
        async with self._checkpointer_context_factory() as checkpointer:
            backend = (
                None
                if self._workspace_root is None
                else _macro_research_backend(
                    workspace_root=self._workspace_root,
                    scope=scope,
                )
            )
            graph = self._agent_factory(
                model=self._model,
                tools=evidence_tools,
                system_prompt=_PARENT_SYSTEM_PROMPT,
                subagents=subagents,
                response_format=ToolStrategy(MacroResearchArtifactDraft),
                checkpointer=checkpointer,
                name="macro-research-parent",
                **({"backend": backend} if backend is not None else {}),
            )
            invocation = {
                "messages": [
                    {
                        "role": "user",
                        "content": (
                            f"自主研究 completed session={scope.session_date.isoformat()}，"
                            f"market_cutoff_ms={scope.market_cutoff_ms}，"
                            f"sealed_at_ms={scope.sealed_at_ms}，"
                            f"evidence_pack_id={scope.evidence_pack_id}，scope_id={scope.scope_id}。"
                            "请先规划，再按需检索、委派、寻找反证和审阅，最终返回结构化研究 artifact。"
                        ),
                    }
                ]
            }
            thread_config = {
                "configurable": {
                    "thread_id": scope.scope_id,
                }
            }
            checkpoint = await checkpointer.aget_tuple(thread_config)
            raw_result = await graph.ainvoke(
                # LangGraph treats new messages as a new input and discards
                # unfinished tasks; None resumes the latest native checkpoint.
                None if checkpoint is not None else invocation,
                config=thread_config,
            )

        draft = _structured_draft(raw_result)
        tool_session.hydrate_final_citations(
            tuple(selection.source_ref for selection in draft.citations),
        )
        artifact = canonicalize_macro_research_artifact(
            draft,
            disclosed_evidence=tool_session.disclosed,
        )
        require_artifact_integrity(
            artifact,
            scope=scope,
            verified_evidence_refs=frozenset(tool_session.disclosed),
        )
        message_tool_calls, subagents_used, model_calls = _message_audit(raw_result.get("messages", ()))
        return MacroResearchAgentResult(
            artifact=artifact,
            audit=MacroResearchAudit(
                scope_id=scope.scope_id,
                deepagents_version=str(deepagents.__version__),
                model_name=self._model_name,
                prompt_version=MACRO_RESEARCH_PROMPT_VERSION,
                workflow_version=MACRO_RESEARCH_WORKFLOW_VERSION,
                model_calls=model_calls,
                tool_calls=tuple((*tool_session.call_names, *message_tool_calls)),
                subagents=subagents_used,
                verified_source_refs=tuple(tool_session.disclosed),
            ),
        )


class _EvidenceToolSession:
    def __init__(
        self,
        *,
        scope: FrozenMacroEvidenceScope,
        reader: MacroResearchReadPort,
    ) -> None:
        self.scope = scope
        self.reader = reader
        self.call_names: list[str] = []
        self.disclosed: dict[str, MacroEvidenceRecord] = {}

    def tools(self) -> tuple[BaseTool, ...]:
        scope = self.scope
        reader = self.reader
        record_call = self.call_names.append

        @tool("inspect_macro_evidence_catalog")
        def inspect_macro_evidence_catalog() -> str:
            """Inspect bounded concept/source counts for this frozen research scope."""

            record_call("inspect_macro_evidence_catalog")
            catalog = require_catalog_in_scope(scope, reader.catalog(scope=scope))
            return _tool_payload(
                {
                    "scope_id": scope.scope_id,
                    "catalog": catalog.model_dump(mode="json"),
                }
            )

        @tool("search_macro_evidence")
        def search_macro_evidence(
            query: str = "",
            concept_keys: list[str] | None = None,
            start_date: str | None = None,
            end_date: str | None = None,
            limit: int = 20,
            offset: int = 0,
        ) -> str:
            """Search the frozen Evidence Pack; use next_offset to continue a broad query."""

            record_call("search_macro_evidence")
            resolved_query = MacroEvidenceQuery(
                query=query,
                concept_keys=tuple(concept_keys or ()),
                start_date=_optional_date(start_date),
                end_date=_optional_date(end_date),
                limit=limit,
                offset=offset,
            )
            records = require_evidence_in_scope(
                scope,
                tuple(
                    reader.search_evidence(
                        scope=scope,
                        query=resolved_query,
                    )
                )[: resolved_query.limit],
            )
            self._disclose(records)
            return _bounded_search_payload(
                scope_id=scope.scope_id,
                query=resolved_query.model_dump(mode="json"),
                records=records,
            )

        @tool("read_macro_evidence")
        def read_macro_evidence(source_refs: list[str]) -> str:
            """Read full frozen evidence records by source_ref; no SQL is accepted."""

            record_call("read_macro_evidence")
            requested = _bounded_source_refs(source_refs)
            records = require_evidence_in_scope(
                scope,
                reader.read_evidence(scope=scope, source_refs=requested),
            )
            returned_refs = {record.evidence_ref for record in records}
            unexpected = sorted(returned_refs - set(requested))
            if unexpected:
                raise MacroResearchIntegrityError(
                    "macro_research_reader_returned_unrequested_evidence:" + ",".join(unexpected)
                )
            self._disclose(records)
            return _tool_payload(
                {
                    "scope_id": scope.scope_id,
                    "result_mode": "full_evidence_records",
                    "results": [record.model_dump(mode="json") for record in records],
                    "missing_source_refs": sorted(set(requested) - returned_refs),
                    "guidance": (
                        "Large exact reads are offloaded by the native DeepAgents "
                        "filesystem middleware; use read_file on the returned path."
                    ),
                }
            )

        @tool("read_prior_macro_research")
        def read_prior_macro_research(limit: int = 3, offset: int = 0) -> str:
            """Page prior publications as context, not evidence."""

            record_call("read_prior_macro_research")
            if not 1 <= limit <= MACRO_RESEARCH_MAX_PRIOR_PUBLICATIONS_PER_PAGE:
                raise ValueError("macro_research_prior_limit_out_of_bounds")
            if offset < 0:
                raise ValueError("macro_research_prior_offset_out_of_bounds")
            records = require_prior_research_in_scope(
                scope,
                tuple(
                    reader.prior_research(
                        scope=scope,
                        limit=limit,
                        offset=offset,
                    )
                )[:limit],
            )
            return _tool_payload(
                {
                    "scope_id": scope.scope_id,
                    "results": [record.model_dump(mode="json") for record in records],
                    "returned_count": len(records),
                    "next_offset": offset + len(records) if len(records) == limit else None,
                    "citation_policy": "context_only_not_citable",
                }
            )

        return (
            inspect_macro_evidence_catalog,
            search_macro_evidence,
            read_macro_evidence,
            read_prior_macro_research,
        )

    def _disclose(self, records: Sequence[MacroEvidenceRecord]) -> None:
        for record in records:
            existing = self.disclosed.get(record.evidence_ref)
            if existing is not None and existing != record:
                raise MacroResearchIntegrityError("macro_research_evidence_identity_conflict:" + record.evidence_ref)
            self.disclosed.setdefault(record.evidence_ref, record)

    def hydrate_final_citations(self, source_refs: Sequence[str]) -> None:
        """Re-read final refs so native subagent checkpoints need no root-message copy."""

        requested = tuple(dict.fromkeys(str(source_ref).strip() for source_ref in source_refs))
        if any(not source_ref for source_ref in requested):
            raise MacroResearchIntegrityError("macro_research_final_citation_ref_empty")
        if not requested:
            return
        self.call_names.append("hydrate_final_citations")
        hydrated: list[MacroEvidenceRecord] = []
        for offset in range(0, len(requested), MACRO_RESEARCH_MAX_READ_REFS):
            batch = requested[offset : offset + MACRO_RESEARCH_MAX_READ_REFS]
            records = require_evidence_in_scope(
                self.scope,
                self.reader.read_evidence(
                    scope=self.scope,
                    source_refs=batch,
                ),
            )
            returned_refs = {record.evidence_ref for record in records}
            unexpected = sorted(returned_refs - set(batch))
            if unexpected:
                raise MacroResearchIntegrityError(
                    "macro_research_reader_returned_unrequested_evidence:" + ",".join(unexpected)
                )
            batch_missing = sorted(set(batch) - returned_refs)
            if batch_missing:
                raise MacroResearchIntegrityError(
                    "macro_research_artifact_unknown_citations:" + ",".join(batch_missing)
                )
            hydrated.extend(records)
        self._disclose(hydrated)


def _structured_draft(result: Mapping[str, Any]) -> MacroResearchArtifactDraft:
    raw = result.get("structured_response")
    if raw is None:
        raise RuntimeError("macro_research_structured_response_missing")
    if isinstance(raw, MacroResearchArtifactDraft):
        return raw
    return MacroResearchArtifactDraft.model_validate(raw)


def _macro_research_backend(
    *,
    workspace_root: Path,
    scope: FrozenMacroEvidenceScope,
) -> CompositeBackend:
    scope_suffix = scope.scope_id.rsplit(":", maxsplit=1)[-1]
    execution_root = workspace_root / f"{scope.session_date.isoformat()}-{scope_suffix}"
    execution_root.mkdir(parents=True, exist_ok=True)
    command_path = os.pathsep.join(
        (
            str(Path(sys.executable).resolve().parent),
            "/usr/local/bin",
            "/usr/bin",
            "/bin",
        )
    )

    execution_backend = LocalShellBackend(
        root_dir=execution_root,
        virtual_mode=True,
        timeout=120,
        max_output_bytes=10_000_000,
        env={
            "PATH": command_path,
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PYTHONIOENCODING": "utf-8",
        },
        inherit_env=False,
    )
    return CompositeBackend(
        default=execution_backend,
        routes={
            "/workspace/": execution_backend,
            "/": StateBackend(),
        },
        artifacts_root="/",
    )


def _message_audit(
    messages: Any,
) -> tuple[tuple[str, ...], tuple[str, ...], int]:
    tool_names: list[str] = []
    subagents: list[str] = []
    model_calls = 0
    for message in messages if isinstance(messages, Sequence) else ():
        if not isinstance(message, AIMessage):
            continue
        model_calls += 1
        for call in message.tool_calls:
            name = str(call.get("name") or "")
            if name:
                tool_names.append(name)
            if name != "task":
                continue
            args = call.get("args")
            if not isinstance(args, Mapping):
                continue
            subagent_name = str(args.get("subagent_type") or "").strip()
            if subagent_name and subagent_name not in subagents:
                subagents.append(subagent_name)
    return tuple(tool_names), tuple(subagents), model_calls


def _bounded_source_refs(raw_refs: Sequence[str]) -> tuple[str, ...]:
    refs = tuple(str(item).strip() for item in raw_refs)
    if not refs or any(not item for item in refs):
        raise ValueError("macro_research_source_refs_required")
    if len(refs) > MACRO_RESEARCH_MAX_READ_REFS:
        raise ValueError("macro_research_source_refs_limit_exceeded")
    if len(refs) != len(set(refs)):
        raise ValueError("macro_research_duplicate_source_ref")
    return refs


def _optional_date(raw: str | None) -> date | None:
    if raw is None:
        return None
    normalized = str(raw).strip()
    if not normalized:
        return None
    try:
        return date.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError("macro_research_query_date_invalid") from exc


def _tool_payload(value: Mapping[str, Any]) -> str:
    return _encode_tool_payload(value)


def _bounded_search_payload(
    *,
    scope_id: str,
    query: Mapping[str, Any],
    records: Sequence[MacroEvidenceRecord],
) -> str:
    hits = tuple(_search_hit(record) for record in records)
    packed: list[dict[str, Any]] = []
    for hit in hits:
        candidate = (*packed, hit)
        if (
            len(
                _encode_tool_payload(
                    _search_response(
                        scope_id=scope_id,
                        query=query,
                        results=candidate,
                        available_count=len(hits),
                    )
                )
            )
            > _SEARCH_PAYLOAD_TARGET_CHARS
        ):
            break
        packed.append(hit)
    return _tool_payload(
        _search_response(
            scope_id=scope_id,
            query=query,
            results=tuple(packed),
            available_count=len(hits),
        )
    )


def _search_response(
    *,
    scope_id: str,
    query: Mapping[str, Any],
    results: Sequence[Mapping[str, Any]],
    available_count: int,
) -> dict[str, Any]:
    offset = int(query.get("offset") or 0)
    limit = int(query.get("limit") or 0)
    truncated_by_payload = len(results) < available_count
    more_results_possible = truncated_by_payload or available_count >= limit
    return {
        "scope_id": scope_id,
        "query": dict(query),
        "result_mode": "compact_search_hits",
        "results": list(results),
        "returned_count": len(results),
        "truncated_by_payload": truncated_by_payload,
        "next_offset": offset + len(results) if more_results_possible and results else None,
        "guidance": (
            "Use read_macro_evidence for selected source_ref values. "
            "If next_offset is present, repeat the same search with that offset."
        ),
    }


def _search_hit(record: MacroEvidenceRecord) -> dict[str, Any]:
    payload = record.payload
    fact_keys = (
        (
            "concept_key",
            "series_key",
            "value_numeric",
            "unit",
            "frequency",
            "data_quality",
            "availability",
        )
        if record.evidence_kind == "observation"
        else ("title", "summary", "language", "lifecycle_status")
    )
    return {
        "source_ref": record.evidence_ref,
        "evidence_kind": record.evidence_kind,
        "source_label": record.source_label,
        "concept_key": record.concept_key,
        "source_timestamp": record.source_timestamp,
        "available_at_ms": record.available_at_ms,
        "observed_at": record.observed_at.isoformat() if record.observed_at else None,
        "published_at_ms": record.published_at_ms,
        "url": record.url,
        "summary": record.summary[:2_000],
        "facts": {key: _compact_fact_value(payload[key]) for key in fact_keys if key in payload},
        "lineage": dict(record.lineage),
    }


def _encode_tool_payload(value: Mapping[str, Any]) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _compact_fact_value(value: Any) -> Any:
    if isinstance(value, str):
        return value[:2_000]
    return value


__all__ = [
    "MACRO_RESEARCH_PROMPT_VERSION",
    "MACRO_RESEARCH_TOOL_NAMES",
    "MACRO_RESEARCH_WORKFLOW_VERSION",
    "MacroResearchDeepAgent",
]
