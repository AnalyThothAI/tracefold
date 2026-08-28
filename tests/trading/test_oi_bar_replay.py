from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace

from tracefold.app.cli.commands import trading_replay as trading_replay_command
from tracefold.integrations.venues import VenueBar
from tracefold.trading import (
    BlacklistSnapshotV1,
    ExecutionCapabilitySnapshotV1,
    ExecutionInstrumentCapabilityV1,
    InstrumentRef,
    ReplayBarV1,
    replay,
)
from tracefold.trading.candidate.blacklist import CanonicalBlacklistEntryV1
from tracefold.trading.contracts import Bar, OiTradeCandidate, RegimeAssessment
from tracefold.trading.replay import BarEpisodeResult, DirectionalReplayPlan, ReplayMarketSlice

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

    market_slice = asyncio.run(trading_replay_command._fetch_market_slices([plan], now_ms=NOW))[0]

    assert market_slice.end_ms > NOW
    assert [bar.close_at_ms for bar in market_slice.bars] == [NOW]


def test_blacklist_denies_capital_without_rewriting_directional_alpha(monkeypatch) -> None:
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
    bars = [
        ReplayBarV1(
            venue="binance.perp",
            instrument_id=INSTRUMENT_ID,
            open_at_ms=NOW - 300_000,
            close_at_ms=NOW,
            open="0.1000",
            high="0.1010",
            low="0.0990",
            close="0.1000",
            volume="10000",
        )
    ]
    market_slice = ReplayMarketSlice(plan, bars, None, NOW - 300_000, NOW + 1_200_000)
    strategy = SimpleNamespace(
        strategy_id="oi_smart_money_momentum_v1",
        strategy_version="oi_smart_money_momentum_v1",
        config_digest="3" * 64,
        evaluate=lambda _context: SimpleNamespace(
            decision="long",
            rule="smart_money_momentum_long",
            model_dump=lambda **_kwargs: {"decision": "long", "rule": "smart_money_momentum_long"},
        ),
    )
    episode_calls: list[str] = []
    monkeypatch.setattr(
        replay,
        "select_bar",
        lambda *_args, **_kwargs: Bar(open_at_ms=NOW - 300_000, close_at_ms=NOW, close="0.1000"),
    )
    monkeypatch.setattr(replay, "pre_move_bps", lambda *_args, **_kwargs: 100)
    monkeypatch.setattr(
        replay,
        "assess",
        lambda **_kwargs: RegimeAssessment(
            regime="buildup_up",
            reason="confirmed",
            pre_move_bps=100,
            oi_direction="rise",
        ),
    )

    def run_episode(**_kwargs) -> BarEpisodeResult:
        episode_calls.append("ran")
        return BarEpisodeResult("CLOSED", "max_holding")

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

    outcome = replay.evaluate_replay_market_slices(
        [market_slice],
        strategy=strategy,
        snapshot=_snapshot(),
        blacklist=blacklist,
        run_episode=run_episode,
    )[0]

    assert episode_calls == ["ran"]
    assert (outcome.decision, outcome.execution) == ("DIRECTIONAL", "CLOSED")
    assert (outcome.capital_admission, outcome.capital_reason) == ("DENIED", "blacklisted")
    assert outcome.replay_intent is not None
