"""Provider JSON enters the strict proposal without asking for caller-owned digests."""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from types import SimpleNamespace

import dspy
import pytest

from tests.trading.test_analysis_core import _assessment
from tracefold.app.llm import ConfiguredLMEndpoint
from tracefold.app.trading_analyst import TradeAnalyst, _physical_call, _WireAssessment
from tracefold.platform.config.models import TradingAnalysisSettings
from tracefold.trading.engine.brief import AnalystBrief


def test_agent_wire_arrays_become_strict_assessment() -> None:
    original = _assessment(evidence="market:perp_bars")

    class Predictor:
        async def acall(self, **kwargs: object) -> SimpleNamespace:
            assert "brief_sha" not in kwargs
            assert "candidate_menu_sha" not in kwargs
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
                sha="a" * 64,
                candidate_menu_sha="b" * 64,
                evidence_catalog={},
            )
        )
    )
    assert receipt.status == "provider_success"
    assert receipt.assessment == original
    assert receipt.request_payload is not None
    assert receipt.request_payload["brief_sha"] == "a" * 64


def test_zero_reported_tokens_remain_known() -> None:
    call = _physical_call(
        {
            "messages": [],
            "response": {},
            "usage": {"prompt_tokens": 0, "completion_tokens": 0},
            "cost": "0",
        }
    )
    assert (call.input_tokens, call.output_tokens, call.cost_microusd) == (0, 0, 0)


def test_invalid_answer_retains_all_physical_calls_and_unknown_total_cost() -> None:
    original = _assessment(evidence="market:perp_bars")

    class Predictor:
        async def acall(self, **kwargs: object) -> SimpleNamespace:
            lm = kwargs["lm"]
            lm.history.append(
                {
                    "messages": [{"role": "user", "content": "first"}],
                    "response": {"attempt": 1},
                    "usage": {"prompt_tokens": 10, "completion_tokens": 20},
                    "cost": "0.001",
                }
            )
            lm.history.append(
                {
                    "messages": [{"role": "user", "content": "retry"}],
                    "response": {"attempt": 2},
                    "usage": {"prompt_tokens": 12, "completion_tokens": 22},
                    "cost": None,
                }
            )
            raw = json.loads(original.model_dump_json())
            raw["action"] = "WATCH"
            raw["entry_candidate_id"] = "forbidden-on-watch"
            return SimpleNamespace(assessment=_WireAssessment.model_validate(raw))

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
                sha="a" * 64,
                candidate_menu_sha="b" * 64,
                evidence_catalog={},
            )
        )
    )
    assert receipt.error_code == "model_schema_invalid"
    assert receipt.assessment is None
    assert receipt.physical_calls[0].cost_microusd == 1000
    assert receipt.physical_calls[1].cost_microusd is None
    assert receipt.input_tokens == 22 and receipt.output_tokens == 42
    assert receipt.cost_microusd is None
    assert receipt.known_cost_microusd == 1000
    assert receipt.unknown_cost_calls == 1
    assert receipt.cost_upper_estimate_microusd is None
    assert receipt.validation_errors


def test_provider_failure_retains_transport_attempt_without_dspy_history(monkeypatch: pytest.MonkeyPatch) -> None:
    class Predictor:
        async def acall(self, **kwargs: object) -> None:
            await kwargs["lm"].aforward(messages=[{"role": "user", "content": "frozen brief"}])

    async def fail_provider(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("provider response may contain secret text")

    monkeypatch.setattr(dspy.LM, "aforward", fail_provider)
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
        analyst.assess(AnalystBrief(text="{}", sha="a" * 64, candidate_menu_sha="b" * 64, evidence_catalog={}))
    )
    assert receipt.status == "provider_error"
    assert len(receipt.physical_calls) == 1
    assert receipt.physical_calls[0].request_payload["messages"][0]["content"] == "frozen brief"
    assert receipt.physical_calls[0].response_payload is None
    assert receipt.physical_calls[0].status == "result_unknown"
    assert receipt.physical_calls[0].finished_at_ms is not None
    assert receipt.physical_calls[0].error_type == "RuntimeError"
    assert receipt.physical_calls[0].cost_microusd is None
    assert "secret text" not in str(receipt.physical_calls)


def test_transport_attempts_keep_success_then_failure_order(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = 0

    class Predictor:
        async def acall(self, **kwargs: object) -> None:
            lm = kwargs["lm"]
            await lm.aforward(messages=[{"role": "user", "content": "first"}])
            lm.history.append(
                {
                    "messages": [{"role": "user", "content": "first"}],
                    "response": {"ok": True},
                    "usage": {"prompt_tokens": 3, "completion_tokens": 4},
                    "cost": "0.000002",
                }
            )
            await lm.aforward(messages=[{"role": "user", "content": "second"}])

    class FakeResponse:
        def __init__(self) -> None:
            self.usage = {"prompt_tokens": 3, "completion_tokens": 4}
            self._hidden_params = {"response_cost": Decimal("0.000002")}

        def model_dump(self, *, mode: str) -> dict[str, bool]:
            assert mode == "json"
            return {"ok": True}

    async def provider(*_args: object, **_kwargs: object) -> FakeResponse:
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            raise RuntimeError("failed")
        return FakeResponse()

    monkeypatch.setattr(dspy.LM, "aforward", provider)
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
        analyst.assess(AnalystBrief(text="{}", sha="a" * 64, candidate_menu_sha="b" * 64, evidence_catalog={}))
    )
    assert len(receipt.physical_calls) == 2
    assert receipt.physical_calls[0].cost_microusd == 2
    assert receipt.physical_calls[1].request_payload["messages"][0]["content"] == "second"
    assert receipt.physical_calls[1].cost_microusd is None
    assert receipt.cost_microusd is None


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
                evidence_catalog={},
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
            sha="a" * 64,
            candidate_menu_sha="b" * 64,
            evidence_catalog={},
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
                evidence_catalog={},
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


def test_adapter_copy_shares_physical_call_ledger_and_dispatch_callbacks(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[tuple[str, int]] = []

    class Predictor:
        async def acall(self, **kwargs: object) -> SimpleNamespace:
            lm = kwargs["lm"]
            await lm.aforward(messages=[{"role": "user", "content": "first"}])
            fallback = lm.copy()
            await fallback.aforward(messages=[{"role": "user", "content": "fallback"}])
            return SimpleNamespace(assessment=_WireAssessment.model_validate(_assessment().model_dump()))

    class Response:
        def __init__(self) -> None:
            self.usage = {"prompt_tokens": 2, "completion_tokens": 3}
            self._hidden_params = {"response_cost": Decimal("0.000001")}

        def model_dump(self, *, mode: str) -> dict[str, bool]:
            assert mode == "json"
            return {"ok": True}

    async def provider(*_args: object, **_kwargs: object) -> Response:
        return Response()

    async def before_call(index: int, *_args: object) -> None:
        events.append(("before", index))

    async def after_call(index: int, _call: object) -> None:
        events.append(("after", index))

    monkeypatch.setattr(dspy.LM, "aforward", provider)
    analyst = TradeAnalyst(
        ConfiguredLMEndpoint(
            model_name="openai/test-model", api_key="fixture", api_base="http://localhost:1/v1", model_kwargs={}
        ),
        predictor=Predictor(),
    )
    receipt = asyncio.run(
        analyst.assess(
            AnalystBrief(text="{}", sha="a" * 64, candidate_menu_sha="b" * 64, evidence_catalog={}),
            before_call=before_call,
            after_call=after_call,
        )
    )
    assert events == [("before", 0), ("after", 0), ("before", 1), ("after", 1)]
    assert len(receipt.physical_calls) == 2
    assert receipt.cost_microusd == 2
