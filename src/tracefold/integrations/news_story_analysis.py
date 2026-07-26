from __future__ import annotations

import json
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage

from tracefold.news import (
    NEWS_ANALYSIS_PROMPT_VERSION,
    NEWS_ANALYSIS_SCHEMA_VERSION,
    NEWS_ANALYSIS_WORKFLOW_VERSION,
    NewsAnalysisEvidence,
    NewsStoryAnalysisDraft,
    NewsStoryAnalysisResult,
    NewsStoryAnalyzer,
)

_SYSTEM_PROMPT = """你是 Tracefold 全球政治经济新闻分析员。
你只分析输入的一个 Story 及其成员 Article，不搜索、不补充外部事实。

严格规则：
1. 只把 provenance_status=verified 的内容当作已验证来源事实。
2. attributed 和 unknown 必须在“分歧与未知”中明确标注，不得升级为已确认事实。
3. 区分“发生了什么”“政治影响”“经济与市场影响”，不要给交易指令。
4. 每个重要判断都要能回到输入 article_id；evidence_references 只能使用输入中的 article_id。
5. 信息不足时直接写未知，并给出可以验证判断的下一检查点。
6. 输出自然、紧凑、专业的中文结构化结果。
7. 只返回一个符合给定 schema 的 JSON 对象，不要 Markdown、代码围栏或额外解释。
"""


class DeepSeekStoryAnalyzer(NewsStoryAnalyzer):
    def __init__(self, *, model: BaseChatModel, model_name: str) -> None:
        self._model_name = str(model_name).strip()
        if not self._model_name:
            raise ValueError("news_story_analysis_model_name_required")
        self._model = model

    async def analyze(self, evidence: NewsAnalysisEvidence) -> NewsStoryAnalysisResult:
        payload = evidence.model_dump(mode="json")
        response = await self._model.ainvoke(
            [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        "请根据以下冻结 Story evidence 生成结构化中文分析。"
                        "不要使用输入之外的事实。\nJSON schema:\n"
                        + json.dumps(
                            NewsStoryAnalysisDraft.model_json_schema(),
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                        + "\nStory evidence:\n"
                        + json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                    ),
                },
            ]
        )
        if not isinstance(response, AIMessage):
            raise ValueError("news_story_analysis_message_required")
        draft = NewsStoryAnalysisDraft.model_validate(_json_object(_message_text(response)))
        return NewsStoryAnalysisResult(
            draft=draft,
            receipt=_receipt(response, model_name=self._model_name),
        )


def _message_text(message: AIMessage) -> str:
    content = message.content
    if isinstance(content, str):
        text = content.strip()
    elif isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(str(block["text"]))
        text = "\n".join(parts).strip()
    else:
        text = ""
    if not text:
        raise ValueError("news_story_analysis_empty_response")
    return text


def _json_object(text: str) -> dict[str, Any]:
    normalized = text.strip()
    if normalized.startswith("```"):
        lines = normalized.splitlines()
        if len(lines) < 3 or lines[-1].strip() != "```":
            raise ValueError("news_story_analysis_json_fence_invalid")
        normalized = "\n".join(lines[1:-1]).strip()
    try:
        payload = json.loads(normalized)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"news_story_analysis_json_invalid:{exc.msg}:{exc.lineno}:{exc.colno}"
        ) from exc
    if not isinstance(payload, dict):
        raise ValueError("news_story_analysis_json_object_required")
    return payload


def _receipt(raw: object, *, model_name: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model_name,
        "prompt_version": NEWS_ANALYSIS_PROMPT_VERSION,
        "workflow_version": NEWS_ANALYSIS_WORKFLOW_VERSION,
        "schema_version": NEWS_ANALYSIS_SCHEMA_VERSION,
    }
    if isinstance(raw, AIMessage):
        if raw.id:
            payload["response_id"] = str(raw.id)
        response_metadata = dict(raw.response_metadata or {})
        for field_name in ("model_name", "finish_reason", "system_fingerprint"):
            if response_metadata.get(field_name) is not None:
                payload[field_name] = response_metadata[field_name]
        if raw.usage_metadata:
            payload["usage"] = {
                key: int(value)
                for key, value in dict(raw.usage_metadata).items()
                if isinstance(value, int) and not isinstance(value, bool)
            }
    return payload


__all__ = ["DeepSeekStoryAnalyzer"]
