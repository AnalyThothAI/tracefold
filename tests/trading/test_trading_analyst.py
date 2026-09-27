"""The production Trading entry uses native ReAct and canonical LM requests."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace

import dspy
import pytest
from pydantic import ValidationError

from tests.support.scripted_lm import ScriptedLM
from tracefold.app.llm import ConfiguredLMEndpoint
from tracefold.app.trading_analyst import TradeAnalyst, _WireProposal
from tracefold.trading.engine.brief import AnalystBrief
from tracefold.trading.engine.plans import AnalysisProposal
from tracefold.trading.engine.policy import InvalidAssessment


def _endpoint() -> ConfiguredLMEndpoint:
    return ConfiguredLMEndpoint(
        model_name="scripted/test",
        api_key="fixture",
        api_base="http://localhost:1/v1",
        model_kwargs={},
    )


def _brief() -> AnalystBrief:
    return AnalystBrief(text="{}", sha="a" * 64, plan_menu_sha="b" * 64, evidence_catalog={})


def _proposal() -> dict:
    return {
        "selected_plan_id": None,
        "supporting_evidence": [],
        "opposing_evidence": [],
        "judgment_refs": [],
        "limitations": "No citable market snapshot.",
        "public_rationale": "The available data do not support an entry.",
    }


def test_native_react_finish_and_extract_use_two_physical_requests() -> None:
    async def exercise() -> None:
        delegate = ScriptedLM(
            [
                {"next_thought": "Enough information.", "next_tool_name": "finish", "next_tool_args": {}},
                {"reasoning": "No plan selected.", "proposal": _proposal()},
            ]
        )
        analyst = TradeAnalyst(_endpoint(), delegate=delegate)
        receipt = await analyst.assess(_brief())
        assert receipt.status == "provider_success", receipt.error_code
        assert receipt.termination_reason == "finish"
        assert receipt.assessment is not None and receipt.assessment.selected_plan_id is None
        assert len(receipt.physical_calls) == 2
        assert [call.phase for call in receipt.physical_calls] == ["react", "extract"]
        assert all(call.status == "completed" for call in receipt.physical_calls)
        assert all(call.input_tokens == 0 and call.output_tokens == 0 for call in receipt.physical_calls)
        assert delegate.requests[0].system is not None
        assert "selected_plan_id" in delegate.requests[0].system
        assert "selected_plan_id" in delegate.requests[1].system
        await analyst.aclose()

    asyncio.run(exercise())


def test_iteration_limit_allows_valid_extract_without_finish() -> None:
    async def noop() -> str:
        return "ok"

    async def exercise() -> None:
        delegate = ScriptedLM(
            [{"next_thought": "Check.", "next_tool_name": "noop", "next_tool_args": {}} for _ in range(6)]
            + [{"reasoning": "Still insufficient.", "proposal": _proposal()}]
        )
        analyst = TradeAnalyst(_endpoint(), delegate=delegate)
        receipt = await analyst.assess(_brief(), tools=[dspy.Tool(noop)])
        assert receipt.status == "provider_success", receipt.error_code
        assert receipt.termination_reason == "iteration_limit"
        assert len(receipt.physical_calls) == 7
        await analyst.aclose()

    asyncio.run(exercise())


def test_early_final_extract_remains_eligible() -> None:
    class EarlyExit:
        async def acall(self, **_kwargs: object) -> SimpleNamespace:
            return SimpleNamespace(
                trajectory={"tool_name_0": "noop"},
                proposal=_WireProposal.model_validate(_proposal()),
            )

    async def exercise() -> None:
        analyst = TradeAnalyst(_endpoint(), react_factory=lambda *_args, **_kwargs: EarlyExit())
        receipt = await analyst.assess(_brief())
        assert receipt.assessment is not None
        assert receipt.termination_reason == "early_final"
        await analyst.aclose()

    asyncio.run(exercise())


def test_invalid_plan_gets_one_recorded_correction() -> None:
    class InvalidFirst:
        async def acall(self, **_kwargs: object) -> SimpleNamespace:
            return SimpleNamespace(
                trajectory={"tool_name_0": "finish"},
                proposal=_WireProposal.model_validate({**_proposal(), "selected_plan_id": "invented"}),
            )

    async def exercise() -> None:
        delegate = ScriptedLM([{"proposal": _proposal()}])
        analyst = TradeAnalyst(_endpoint(), delegate=delegate, react_factory=lambda *_args, **_kwargs: InvalidFirst())

        def compile_candidate(proposal: AnalysisProposal) -> None:
            if proposal.selected_plan_id is not None:
                raise InvalidAssessment("proposal_plan_outside_menu")

        receipt = await analyst.assess(
            _brief(),
            compile_candidate=compile_candidate,
            correction_catalog=lambda: {"plans": [], "evidence_refs": [], "judgment_refs": []},
        )
        assert receipt.status == "provider_success", receipt.error_code
        assert receipt.assessment is not None and receipt.assessment.selected_plan_id is None
        assert receipt.response_payload is not None
        assert receipt.response_payload["original_candidate"]["selected_plan_id"] == "invented"
        assert len(receipt.physical_calls) == 1
        assert len(delegate.requests) == 1
        await analyst.aclose()

    asyncio.run(exercise())


def test_missing_selection_field_is_invalid_not_no_trade() -> None:
    incomplete = {key: value for key, value in _proposal().items() if key != "selected_plan_id"}
    with pytest.raises(ValidationError, match="selected_plan_id"):
        _WireProposal.model_validate(incomplete)
    with pytest.raises(ValidationError, match="selected_plan_id"):
        AnalysisProposal.model_validate(incomplete)


def test_persistent_invalid_selection_stops_after_one_correction() -> None:
    class InvalidFirst:
        async def acall(self, **_kwargs: object) -> SimpleNamespace:
            return SimpleNamespace(
                trajectory={"tool_name_0": "finish"},
                proposal=_WireProposal.model_validate({**_proposal(), "selected_plan_id": "invented"}),
            )

    async def exercise() -> None:
        delegate = ScriptedLM([{"proposal": {**_proposal(), "selected_plan_id": "still-invented"}}])
        analyst = TradeAnalyst(_endpoint(), delegate=delegate, react_factory=lambda *_args, **_kwargs: InvalidFirst())

        def compile_candidate(proposal: AnalysisProposal) -> None:
            if proposal.selected_plan_id is not None:
                raise InvalidAssessment("proposal_plan_outside_menu")

        receipt = await analyst.assess(
            _brief(),
            compile_candidate=compile_candidate,
            correction_catalog=lambda: {"plans": [], "evidence_refs": [], "judgment_refs": []},
        )
        assert receipt.status == "invalid_output"
        assert receipt.error_code == "proposal_plan_outside_menu"
        assert receipt.assessment is None
        assert len(receipt.physical_calls) == 1
        assert len(delegate.requests) == 1
        assert receipt.response_payload is not None
        assert receipt.response_payload["original_candidate"]["selected_plan_id"] == "invented"
        await analyst.aclose()

    asyncio.run(exercise())


def test_budget_refuses_dispatch_before_provider_call() -> None:
    async def exercise() -> None:
        delegate = ScriptedLM([])
        analyst = TradeAnalyst(
            _endpoint(),
            delegate=delegate,
            cost_budget_microusd=1,
            input_price_ceiling_usd_per_million=Decimal("1"),
            output_price_ceiling_usd_per_million=Decimal("1"),
        )
        receipt = await analyst.assess(_brief())
        assert receipt.status == "budget_exhausted"
        assert receipt.error_code == "model_cost_budget_exceeded"
        assert delegate.requests == []
        await analyst.aclose()

    asyncio.run(exercise())


def test_model_request_size_reports_input_budget_instead_of_cost_budget() -> None:
    async def exercise() -> None:
        delegate = ScriptedLM([])
        analyst = TradeAnalyst(_endpoint(), delegate=delegate, max_input_bytes=1_024)
        receipt = await analyst.assess(_brief())
        assert receipt.status == "budget_exhausted"
        assert receipt.error_code == "model_input_budget_exceeded"
        assert delegate.requests == []
        await analyst.aclose()

    asyncio.run(exercise())
