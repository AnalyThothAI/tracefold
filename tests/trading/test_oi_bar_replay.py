from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from tests.trading_v3_fixtures import binance_capability
from tracefold.app.cli.commands import trading_replay as trading_replay_command
from tracefold.app.trading_config import capital_lane_config
from tracefold.integrations.nautilus.replay import run_bar_episode
from tracefold.integrations.venues import VenueBar
from tracefold.platform.config.models import Settings
from tracefold.trading import (
    BlacklistSnapshotV1,
    ExecutionCapabilitySnapshotV2,
    InstrumentRef,
    ReplayBarV1,
    replay,
)
from tracefold.trading.blacklist import CanonicalBlacklistEntryV1
from tracefold.trading.contracts import OiTradeCandidate
from tracefold.trading.market_context import PriceWindow
from tracefold.trading.policy import CapitalPolicy, CapitalPolicyConfig
from tracefold.trading.replay import DirectionalReplayPlan, ReplayMarketSlice

NOW = 1_900_000_000_000
INSTRUMENT_ID = "TUTUSDT-PERP.BINANCE"


def _source() -> OiTradeCandidate:
    return OiTradeCandidate(
        event_id="event-tut",
        observed_at_ms=NOW,
        verdict_created_at_ms=NOW + 1,
        base_symbol="TUT",
        venue="binance",
        oi_direction="rise",
        oi_change_bps=1_548,
        oi_value_usd=23_010_000,
        whale_long_profit_bps=9_074,
        whale_oi_ratio_bps=5_424,
        rank_in_window=1,
        final_decision="drop",
        source_rule="whale_ratio_below_threshold",
        metric_version="oi_signal_v1",
        source_strategy_id="1019",
        source_contract_version="opennews_oi_source_v1",
        measurement_window_ms=300_000,
        learning_epoch="program_v9",
        program_version="news_oi_signal_v2",
        program_sha256="a" * 64,
        policy_version="news_triage_policy_v11",
        judgment_contract_version="news_judgment_v2",
        judgment_origin="oi",
        judgment_sha256="c" * 64,
        runtime_manifest_sha="d" * 64,
    )


def _snapshot() -> ExecutionCapabilitySnapshotV2:
    return binance_capability(symbol="TUTUSDT")


def test_market_slice_excludes_a_candle_not_closed_at_captured_now(monkeypatch) -> None:
    plan = DirectionalReplayPlan(
        source=_source(),
        instrument=InstrumentRef(
            exchange_id="binance",
            venue="binance.perp",
            binding="BINANCE_USDM",
            provider_symbol="TUTUSDT",
            base_symbol="TUT",
            instrument_class="crypto",
            quote_asset="USDT",
            observed_at_ms=NOW,
        ),
        venue="binance.perp",
        instrument_id=INSTRUMENT_ID,
    )

    async def fetched(*_args, **_kwargs):
        return (
            VenueBar(NOW - 300_000, NOW, *(Decimal("1") for _ in range(5))),
            VenueBar(NOW, NOW + 300_000, *(Decimal("2") for _ in range(5))),
        )

    monkeypatch.setattr(trading_replay_command, "fetch_binance_bars", fetched)

    window = PriceWindow(lookback_ms=7_200_000)
    market_slice = asyncio.run(trading_replay_command._fetch_market_slices([plan], now_ms=NOW, price_window=window))[0]

    assert market_slice.end_ms > NOW
    assert [bar.close_at_ms for bar in market_slice.bars] == [NOW]
    assert market_slice.start_ms == NOW - window.lookback_ms - window.bar_gap_tolerance_ms


def _directional_market_slice() -> ReplayMarketSlice:
    source = _source()
    plan = DirectionalReplayPlan(
        source=source,
        instrument=InstrumentRef(
            exchange_id="binance",
            venue="binance.perp",
            binding="BINANCE_USDM",
            provider_symbol="TUTUSDT",
            base_symbol="TUT",
            instrument_class="crypto",
            quote_asset="USDT",
            observed_at_ms=NOW,
        ),
        venue="binance.perp",
        instrument_id=INSTRUMENT_ID,
    )
    historic_prices = [
        Decimal("0.0950") - Decimal(index) * Decimal("0.0005")
        if index <= 12
        else Decimal("0.0890") + Decimal(index - 12) * (Decimal("0.0110") / Decimal(12))
        for index in range(25)
    ]
    bars = [
        ReplayBarV1(
            venue="binance.perp",
            instrument_id=INSTRUMENT_ID,
            open_at_ms=NOW - (25 - index) * 300_000,
            close_at_ms=NOW - (24 - index) * 300_000,
            open=price,
            high=price,
            low=price,
            close=price,
            volume="10000",
        )
        for index, price in enumerate(historic_prices)
    ]
    bars.extend(
        ReplayBarV1(
            venue="binance.perp",
            instrument_id=INSTRUMENT_ID,
            open_at_ms=NOW + (index - 1) * 300_000,
            close_at_ms=NOW + index * 300_000,
            open="0.1000",
            high="0.1010",
            low="0.0990",
            close="0.1000",
            volume="10000",
        )
        for index in range(1, 6)
    )
    return ReplayMarketSlice(plan, bars, None, NOW - 7_530_000, NOW + 1_500_000)


def test_the_operator_notional_and_the_policy_band_drive_a_real_replay_outcome() -> None:
    """The two knobs that remain, and they are owned by different things (#331).

    `fixed_notional_usd` is the operator's and sizes the scenario. The price band is the *policy's* and
    is frozen into its identity, so a replay of a tightened band is a replay of a different policy —
    which is exactly why an operator cannot reach it from settings.
    """

    market_slice = _directional_market_slice()
    configured = capital_lane_config(Settings.model_validate({"trading": {"order": {"fixed_notional_usd": "7.5"}}}))
    default = capital_lane_config(Settings())
    assert configured.target_notional_usd == Decimal("7.5")
    assert default.target_notional_usd == Decimal("10")

    blacklist = BlacklistSnapshotV1(
        revision=1,
        active_rows=(
            CanonicalBlacklistEntryV1(
                underlying_key="crypto:TUT",
                reason="operator",
                created_at_ms=NOW - 1,
            ),
        ),
    )

    def evaluate(policy: CapitalPolicy, notional: Decimal) -> replay.ReplayTerminalOutcomeV1:
        return replay.evaluate_replay_market_slices(
            [market_slice],
            policy=policy,
            snapshot=_snapshot(),
            blacklist=blacklist,
            run_episode=run_bar_episode,
            price_window=PriceWindow(lookback_ms=7_200_000),
            target_notional=notional,
        )[0]

    shipped = evaluate(configured.policy, configured.target_notional_usd)
    assert (shipped.decision, shipped.execution) == ("DIRECTIONAL", "CLOSED")
    assert shipped.quantity == Decimal("75")
    # The deny-list is recorded as a capital-admission observation and never edits the Alpha result.
    assert (shipped.capital_admission, shipped.capital_reason) == ("DENIED", "blacklisted")
    assert shipped.replay_intent is not None

    tightened = evaluate(CapitalPolicy(config=CapitalPolicyConfig(max_price_move_bps=100)), Decimal("10"))
    assert (tightened.decision, tightened.decision_reason) == ("NO_TRADE", "move_above_band_chasing")


def test_replay_engine_failure_aborts_without_a_policy_outcome() -> None:
    def failed_episode(**_kwargs):
        raise RuntimeError("replay_instrument_unavailable")

    with pytest.raises(RuntimeError, match="replay_instrument_unavailable"):
        replay.evaluate_replay_market_slices(
            [_directional_market_slice()],
            policy=CapitalPolicy(),
            snapshot=_snapshot(),
            blacklist=BlacklistSnapshotV1(revision=0, active_rows=()),
            run_episode=failed_episode,
            price_window=PriceWindow(lookback_ms=7_200_000),
            target_notional=Decimal("10"),
        )
