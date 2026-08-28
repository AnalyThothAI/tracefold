from __future__ import annotations

import asyncio
from decimal import Decimal

from tracefold.app.cli.commands import trading_replay as trading_replay_command
from tracefold.app.trading_config import trading_config_from_settings, trading_settings_strategies
from tracefold.integrations.nautilus.replay import run_bar_episode
from tracefold.integrations.venues import VenueBar
from tracefold.platform.config.models import Settings
from tracefold.trading import (
    BlacklistSnapshotV1,
    ExecutionCapabilitySnapshotV1,
    ExecutionInstrumentCapabilityV1,
    InstrumentRef,
    ReplayBarV1,
    replay,
)
from tracefold.trading.candidate.blacklist import CanonicalBlacklistEntryV1
from tracefold.trading.contracts import OiTradeCandidate
from tracefold.trading.decision.regime import RegimePolicy
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
        learning_epoch="program_v8",
        program_version="news_oi_signal_v1",
        program_sha256="a" * 64,
        policy_version="news_triage_policy_v10",
        editorial_origin="telemetry_deterministic",
        editorial_sha256="b" * 64,
        scored_judgment_sha256="c" * 64,
        runtime_manifest_sha="d" * 64,
    )


def _snapshot() -> ExecutionCapabilitySnapshotV1:
    return ExecutionCapabilitySnapshotV1(
        app_revision="revision-1",
        app_image_digest="image-1",
        nautilus_wheel_identity="wheel-1",
        news_universe_digest="1" * 64,
        provider_universe_digest="2" * 64,
        included={
            INSTRUMENT_ID: ExecutionInstrumentCapabilityV1(
                instrument_id=INSTRUMENT_ID,
                native_symbol="TUTUSDT",
                underlying_key="crypto:TUT",
                quote_currency="USDT",
                price_precision=4,
                size_precision=0,
                price_increment="0.0001",
                size_increment="1",
                min_quantity="1",
                min_notional="5",
            )
        },
        excluded={},
    )


def test_market_slice_excludes_a_candle_not_closed_at_captured_now(monkeypatch) -> None:
    plan = DirectionalReplayPlan(
        source=_source(),
        instrument=InstrumentRef(
            exchange_id="binance",
            venue="binance.perp",
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

    regime = RegimePolicy(lookback_ms=7_200_000)
    market_slice = asyncio.run(trading_replay_command._fetch_market_slices([plan], now_ms=NOW, regime_policy=regime))[0]

    assert market_slice.end_ms > NOW
    assert [bar.close_at_ms for bar in market_slice.bars] == [NOW]
    assert market_slice.start_ms == NOW - regime.lookback_ms - regime.bar_gap_tolerance_ms


def test_nondefault_settings_drive_real_regime_notional_and_blacklist_outcome() -> None:
    source = _source()
    plan = DirectionalReplayPlan(
        source=source,
        instrument=InstrumentRef(
            exchange_id="binance",
            venue="binance.perp",
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
    market_slice = ReplayMarketSlice(plan, bars, None, NOW - 7_530_000, NOW + 1_500_000)
    configured_settings = Settings.model_validate(
        {
            "trading": {
                "regime": {"lookback_seconds": 7_200, "min_price_move_bps": 75, "max_price_move_bps": 800},
                "order": {"fixed_notional_usd": "7.5"},
            }
        }
    )
    configured = trading_config_from_settings(configured_settings)
    default = trading_config_from_settings(Settings())
    strategy = next(
        item
        for item in trading_settings_strategies(configured_settings)
        if item.strategy_id == "oi_smart_money_momentum_v1"
    )

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

    configured_outcome = replay.evaluate_replay_market_slices(
        [market_slice],
        strategy=strategy,
        snapshot=_snapshot(),
        blacklist=blacklist,
        run_episode=run_bar_episode,
        regime_policy=configured.regime,
        target_notional=configured.fixed_notional_usd,
    )[0]
    default_outcome = replay.evaluate_replay_market_slices(
        [market_slice],
        strategy=strategy,
        snapshot=_snapshot(),
        blacklist=blacklist,
        run_episode=run_bar_episode,
        regime_policy=default.regime,
        target_notional=default.fixed_notional_usd,
    )[0]

    assert (configured_outcome.decision, configured_outcome.execution) == ("DIRECTIONAL", "CLOSED")
    assert configured_outcome.quantity == Decimal("75")
    assert (configured_outcome.capital_admission, configured_outcome.capital_reason) == ("DENIED", "blacklisted")
    assert configured_outcome.replay_intent is not None
    assert (default_outcome.decision, default_outcome.decision_reason) == ("NO_TRADE", "move_above_band_chasing")
