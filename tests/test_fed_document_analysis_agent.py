from __future__ import annotations

import asyncio
import json
from datetime import date

import pytest
from langchain_core.messages import AIMessage

from tracefold.integrations.deepagents.fed_document_analysis import (
    FedDocumentAnalysisAgent,
)
from tracefold.macro import MacroModelExpectedError


class _Model:
    def __init__(self, response: AIMessage) -> None:
        self.response = response
        self.messages: list[object] | None = None

    async def ainvoke(self, messages: list[object]) -> AIMessage:
        self.messages = messages
        return self.response


def _document() -> dict[str, object]:
    return {
        "document_id": "macrodoc_statement",
        "document_type": "statement",
        "title": "FOMC statement",
        "effective_date": date(2026, 6, 17),
        "source_url": "https://www.federalreserve.gov/example",
        "document_hash": "sha256:statement",
        "content_text": "Inflation remains somewhat elevated.",
        "metadata_json": {},
    }


def test_agent_parses_json_text_block_without_native_response_format() -> None:
    payload = {
        "policy_relevance": "policy_signal",
        "stance": "hawkish",
        "confidence": 0.7,
        "change_from_prior": "no_prior",
        "rationale": "Inflation remains elevated.",
        "evidence": [
            {
                "evidence_id": "E0001",
                "claim": "Inflation is still elevated.",
            }
        ],
    }
    response = AIMessage(
        content=[
            {"type": "thinking", "thinking": "internal"},
            {"type": "text", "text": f"```json\n{json.dumps(payload)}\n```"},
        ]
    )
    model = _Model(response)
    agent = FedDocumentAnalysisAgent(model=model, model_name="openai/deepseek-v4-pro")
    submissions: list[bool] = []

    draft = asyncio.run(
        agent.analyze(
            document=_document(),
            roster_context=None,
            prior_analysis=None,
            on_model_submitted=lambda: submissions.append(True),
        )
    )

    assert draft.stance == "hawkish"
    assert draft.evidence[0].excerpt == "Inflation remains somewhat elevated."
    assert model.messages is not None
    assert submissions == [True]


def test_agent_rejects_non_json_response() -> None:
    agent = FedDocumentAnalysisAgent(
        model=_Model(AIMessage(content="I cannot provide JSON.")),
        model_name="test-model",
    )

    with pytest.raises(MacroModelExpectedError, match="macro_document_model_expected"):
        asyncio.run(
            agent.analyze(
                document=_document(),
                roster_context=None,
                prior_analysis=None,
                on_model_submitted=lambda: None,
            )
        )
