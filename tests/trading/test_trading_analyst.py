"""The production Trading entry uses native ReAct and canonical LM requests."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace

import dspy

from tracefold.app.llm import ConfiguredLMEndpoint
from tracefold.app.trading_analyst import TradeAnalyst, _WireProposal
from tracefold.news.program.lm import ScriptedLM
from tracefold.trading.engine.brief import AnalystBrief


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
        "assessment_version": "trade_assessment_v4",
        "action": "NO_TRADE",
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
        assert receipt.assessment is not None and receipt.assessment.action == "NO_TRADE"
        assert len(receipt.physical_calls) == 2
        assert [call.phase for call in receipt.physical_calls] == ["react", "extract"]
        assert all(call.status == "completed" for call in receipt.physical_calls)
        assert all(call.input_tokens == 0 and call.output_tokens == 0 for call in receipt.physical_calls)
        assert delegate.requests[0].system is not None
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


def test_early_unconfirmed_exit_does_not_publish_extracted_trade() -> None:
    class EarlyExit:
        async def acall(self, **_kwargs: object) -> SimpleNamespace:
            return SimpleNamespace(
                trajectory={"tool_name_0": "noop"},
                proposal=_WireProposal.model_validate(_proposal()),
            )

    async def exercise() -> None:
        analyst = TradeAnalyst(_endpoint(), react_factory=lambda *_args, **_kwargs: EarlyExit())
        receipt = await analyst.assess(_brief())
        assert receipt.assessment is None
        assert receipt.error_code == "react_termination_unconfirmed"
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
