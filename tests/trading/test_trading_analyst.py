"""Provider JSON arrays enter the strict frozen assessment without coercing scores."""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from types import SimpleNamespace

import pytest

from tests.trading.test_analysis_core import _assessment
from tracefold.app.llm import ConfiguredLMEndpoint
from tracefold.app.trading_analyst import TradeAnalyst, _WireAssessment
from tracefold.platform.config.models import TradingAnalysisSettings
from tracefold.trading.engine.brief import AnalystBrief


def test_agent_wire_arrays_become_strict_assessment() -> None:
    original = _assessment(evidence="market:perp_bars")

    class Predictor:
        async def acall(self, **kwargs: object) -> SimpleNamespace:
            assert kwargs["brief_sha"] == original.brief_sha
            assert kwargs["candidate_menu_sha"] == original.candidate_menu_sha
            return SimpleNamespace(
                assessment=_WireAssessment.model_validate(
                    json.loads(original.model_dump_json()),
                )
            )

    analyst = TradeAnalyst(
        ConfiguredLMEndpoint(
            model_name="openai/test-model",
            api_key="fixture",
            api_base="http://localhost:1/v1",
            model_kwargs={},
        ),
        predictor=Predictor(),
    )
    receipt = asyncio.run(
        analyst.assess(
            AnalystBrief(
                text="{}",
                sha=original.brief_sha,
                candidate_menu_sha=original.candidate_menu_sha,
                evidence_refs=frozenset({"market:perp_bars"}),
            )
        )
    )
    assert receipt.status == "provider_success"
    assert receipt.assessment == original
    assert receipt.request_payload is not None
    assert receipt.request_payload["brief_sha"] == original.brief_sha


def test_oversized_brief_is_recorded_without_a_model_call() -> None:
    class Predictor:
        async def acall(self, **_kwargs: object) -> None:
            raise AssertionError("model_call_must_not_start")

    analyst = TradeAnalyst(
        ConfiguredLMEndpoint(
            model_name="openai/test-model",
            api_key="fixture",
            api_base="http://localhost:1/v1",
            model_kwargs={},
        ),
        max_input_bytes=4,
        predictor=Predictor(),
    )
    receipt = asyncio.run(
        analyst.assess(
            AnalystBrief(
                text="oversized",
                sha="a" * 64,
                candidate_menu_sha="b" * 64,
                evidence_refs=frozenset(),
            )
        )
    )
    assert (receipt.status, receipt.error_code, receipt.assessment) == (
        "budget_exhausted",
        "model_input_budget_exceeded",
        None,
    )
    assert receipt.request_payload is not None
    assert receipt.request_payload["brief_json"] == "oversized"


def test_model_slot_bounds_concurrency_and_queue_deadline() -> None:
    active = 0
    peak = 0
    entered = asyncio.Event()
    release = asyncio.Event()

    class Predictor:
        async def acall(self, **_kwargs: object) -> SimpleNamespace:
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            entered.set()
            try:
                await release.wait()
            finally:
                active -= 1
            return SimpleNamespace(
                assessment=_WireAssessment.model_validate(
                    json.loads(_assessment(evidence="market:perp_bars").model_dump_json()),
                )
            )

    async def exercise() -> tuple[str, str]:
        original = _assessment(evidence="market:perp_bars")
        analyst = TradeAnalyst(
            ConfiguredLMEndpoint(
                model_name="openai/test-model",
                api_key="fixture",
                api_base="http://localhost:1/v1",
                model_kwargs={},
            ),
            timeout_seconds=0.1,
            max_concurrent_calls=1,
            predictor=Predictor(),
        )
        brief = AnalystBrief(
            text="{}",
            sha=original.brief_sha,
            candidate_menu_sha=original.candidate_menu_sha,
            evidence_refs=frozenset({"market:perp_bars"}),
        )
        first = asyncio.create_task(analyst.assess(brief))
        await entered.wait()
        second = asyncio.create_task(analyst.assess(brief))
        second_receipt = await second
        release.set()
        first_receipt = await first
        return first_receipt.status, second_receipt.status

    first_status, second_status = asyncio.run(exercise())
    assert (first_status, second_status) == ("timeout", "timeout")
    assert peak == 1


def test_configured_cost_bound_refuses_a_paid_call_before_dispatch() -> None:
    class Predictor:
        async def acall(self, **_kwargs: object) -> None:
            raise AssertionError("cost_bound_must_prevent_call")

    with pytest.raises(ValueError, match="trading_analysis_model_cost_budget_incomplete"):
        TradingAnalysisSettings(model_cost_budget_microusd=1, model_input_price_ceiling_usd_per_million=None)
    analyst = TradeAnalyst(
        ConfiguredLMEndpoint(
            model_name="openai/test-model",
            api_key="fixture",
            api_base="http://localhost:1/v1",
            model_kwargs={},
        ),
        cost_budget_microusd=1,
        input_price_ceiling_usd_per_million=Decimal("1"),
        output_price_ceiling_usd_per_million=Decimal("1"),
        predictor=Predictor(),
    )
    receipt = asyncio.run(
        analyst.assess(
            AnalystBrief(
                text="{}",
                sha="a" * 64,
                candidate_menu_sha="b" * 64,
                evidence_refs=frozenset(),
            )
        )
    )
    assert (receipt.status, receipt.error_code, receipt.assessment) == (
        "budget_exhausted",
        "model_cost_budget_exceeded",
        None,
    )
    assert receipt.request_payload is not None
    assert receipt.request_payload["model_cost_admission_bound_microusd"] > 1
