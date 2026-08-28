from __future__ import annotations

from decimal import Decimal

import pytest

from tracefold.integrations.nautilus import replay
from tracefold.trading import ReplayBarV1, ReplayExecutionIntentV1, ReplayScenarioCapabilityV1


def _intent() -> ReplayExecutionIntentV1:
    return ReplayExecutionIntentV1(
        source_identity="source-1",
        case_or_decision_identity="decision-1",
        strategy_identity="strategy-1",
        scenario_venue="binance.perp",
        instrument_id="ETHUSDT-PERP.BINANCE",
        underlying_key="crypto:ETH",
        risk_policy_sha256="1" * 64,
        scenario_capability_sha256="2" * 64,
        ts_event=1_000,
        ts_init=1_000,
    )


def _capability() -> ReplayScenarioCapabilityV1:
    return ReplayScenarioCapabilityV1(
        venue="binance.perp",
        instrument_id="ETHUSDT-PERP.BINANCE",
        native_symbol="ETHUSDT",
        base_currency="ETH",
        quote_currency="USDT",
        price_precision=2,
        size_precision=3,
        price_increment="0.01",
        size_increment="0.001",
        min_quantity="0.001",
        min_notional="5",
        provenance="execution_capability_snapshot",
    )


def test_every_directional_episode_constructs_and_disposes_a_fresh_engine(monkeypatch) -> None:
    lifecycle: list[str] = []

    class _Engine:
        def __init__(self, **_kwargs) -> None:
            lifecycle.append("created")

        def dispose(self) -> None:
            lifecycle.append("disposed")

    monkeypatch.setattr(replay, "BacktestEngine", _Engine)

    first = replay.run_bar_episode(intent=_intent(), capability=_capability(), bars=[], reference_price=100)
    second = replay.run_bar_episode(intent=_intent(), capability=_capability(), bars=[], reference_price=100)

    assert first.reason == second.reason == "outside_bar_coverage"
    assert lifecycle == ["created", "disposed", "created", "disposed"]


@pytest.mark.parametrize(("bar_low", "terminal_reason"), (("0.0990", "max_holding"), ("0.0970", "stop")))
def test_bar_episode_runs_through_the_real_engine_and_closes(
    bar_low: str,
    terminal_reason: str,
) -> None:
    capability = ReplayScenarioCapabilityV1(
        venue="binance.perp",
        instrument_id="DOGEUSDT-PERP.BINANCE",
        native_symbol="DOGEUSDT",
        base_currency="DOGE",
        quote_currency="USDT",
        price_precision=4,
        size_precision=0,
        price_increment="0.0001",
        size_increment="1",
        min_quantity="1",
        min_notional="5",
        provenance="execution_capability_snapshot",
    )
    intent = _intent().model_copy(
        update={
            "instrument_id": capability.instrument_id,
            "underlying_key": "crypto:DOGE",
            "ts_event": 0,
            "ts_init": 300_000,
        }
    )
    bars = [
        ReplayBarV1(
            venue="binance.perp",
            instrument_id=capability.instrument_id,
            open_at_ms=close_at_ms - 300_000,
            close_at_ms=close_at_ms,
            open="0.1000",
            high="0.1010",
            low=bar_low,
            close="0.1000",
            volume="10000",
        )
        for close_at_ms in (300_000, 600_000, 900_000, 1_200_000, 1_500_000)
    ]

    outcome = replay.run_bar_episode(
        intent=intent,
        capability=capability,
        bars=bars,
        reference_price=Decimal("0.1000"),
    )

    assert (outcome.execution, outcome.reason) == ("CLOSED", terminal_reason)
    assert outcome.entry_price is not None
    assert outcome.exit_price is not None
    assert outcome.quantity == Decimal("100")
    assert outcome.fees is not None
    assert outcome.net_excluding_funding is not None
