from __future__ import annotations

import asyncio
from datetime import date
from typing import Any

import pytest
from langchain_core.messages import AIMessage

from tracefold.macro import FedDocumentAnalysisAgent, MacroModelExpectedError
from tracefold.macro.fed_document_agent import FedModelDraft


class _StructuredModel:
    """BaseChatModel double: with_structured_output returns a runnable yielding {"raw", "parsed"}."""

    def __init__(self, parsed: FedModelDraft | None, *, hang: bool = False) -> None:
        self.parsed = parsed
        self.hang = hang
        self.schema: Any = None
        self.messages: list[object] | None = None
        self.cancelled = False

    def with_structured_output(self, schema: Any, **kwargs: Any) -> _StructuredModel:
        self.schema = schema
        self.kwargs = kwargs
        return self

    async def ainvoke(self, messages: list[object]) -> dict[str, Any]:
        self.messages = messages
        if self.hang:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        return {"raw": AIMessage(content=""), "parsed": self.parsed, "parsing_error": None}


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


def _draft(**overrides: Any) -> FedModelDraft:
    fields: dict[str, Any] = {
        "policy_relevance": "policy_signal",
        "stance": "hawkish",
        "confidence": 0.7,
        "change_from_prior": "no_prior",
        "rationale": "Inflation remains elevated.",
        "evidence": [{"evidence_id": "E0001", "claim": "Inflation is still elevated."}],
    }
    fields.update(overrides)
    return FedModelDraft.model_validate(fields)


def _agent(model: _StructuredModel, *, timeout: float = 1.0) -> FedDocumentAnalysisAgent:
    return FedDocumentAnalysisAgent(model=model, model_name="test-model", completion_timeout_seconds=timeout)  # type: ignore[arg-type]


def test_agent_uses_function_calling_structured_output_and_maps_evidence_ids_to_excerpts() -> None:
    model = _StructuredModel(_draft())
    submissions: list[bool] = []

    draft = asyncio.run(
        _agent(model).analyze(
            document=_document(),
            roster_context=None,
            prior_analysis=None,
            on_model_submitted=lambda: submissions.append(True),
        )
    )

    assert model.schema is FedModelDraft and model.kwargs["method"] == "function_calling"
    assert draft.stance == "hawkish"
    assert draft.evidence[0].excerpt == "Inflation remains somewhat elevated."
    assert model.messages is not None and len(model.messages) == 2
    assert submissions == [True]


def test_agent_rejects_missing_structured_output_and_unknown_evidence_ids_as_expected_failures() -> None:
    with pytest.raises(MacroModelExpectedError, match="no_structured_output"):
        asyncio.run(
            _agent(_StructuredModel(None)).analyze(
                document=_document(), roster_context=None, prior_analysis=None, on_model_submitted=lambda: None
            )
        )
    unknown = _draft(evidence=[{"evidence_id": "E0099", "claim": "not in catalog"}])
    with pytest.raises(MacroModelExpectedError, match="unknown_evidence_id"):
        asyncio.run(
            _agent(_StructuredModel(unknown)).analyze(
                document=_document(), roster_context=None, prior_analysis=None, on_model_submitted=lambda: None
            )
        )


def test_agent_bounds_hanging_model_as_expected_failure() -> None:
    model = _StructuredModel(_draft(), hang=True)
    submissions: list[bool] = []

    with pytest.raises(MacroModelExpectedError, match=r"^macro_document_model_expected:TimeoutError$"):
        asyncio.run(
            _agent(model, timeout=0.01).analyze(
                document=_document(),
                roster_context=None,
                prior_analysis=None,
                on_model_submitted=lambda: submissions.append(True),
            )
        )

    assert submissions == [True]
    assert model.cancelled is True


def test_draft_schema_enforces_signal_semantics() -> None:
    with pytest.raises(ValueError, match="fed_policy_signal_requires_stance_confidence_evidence"):
        _draft(stance="no_call")
    with pytest.raises(ValueError, match="fed_non_signal_requires_no_call"):
        _draft(policy_relevance="not_policy_signal", stance="hawkish", confidence=0.5)
