"""The pure rules: canonical identity, deny-list, regime, policy, sizing and the paper exit.

Every test here runs without a database, a clock or a network. That is the point of keeping the rules
pure — the funnel report and the runner apply exactly these functions, so a rule that is wrong here is
wrong in production too.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from tracefold.trading.candidate.blacklist import Blacklist
from tracefold.trading.contracts import (
    ACTIVE_ORDER_STATES,
    Bar,
    ExecutionObservation,
    FrozenMarketContext,
    FrozenStrategyContext,
    InstrumentRef,
    MarketContext,
    NativeProtection,
    NewsTradeCandidate,
    OiRegime,
    OiTradeCandidate,
    PreparedOrder,
    RegimeAssessment,
    RiskRejection,
    TradeDecision,
    canonical_base_symbol,
    underlying_key,
)
from tracefold.trading.decision.regime import RegimePolicy, assess, pre_move_bps
from tracefold.trading.execution.order import (
    OrderPolicy,
    build_payload,
    must_close_at,
    protection_matches,
    realized_bps,
    size_order,
    stop_price_for,
)
from tracefold.trading.execution.paper import evaluate_paper_exit
from tracefold.trading.pipeline.runtime import BAR_INTERVAL_MS, cutoff_history_start_ms
from tracefold.trading.strategy.news_oi_alignment import NewsOiAlignmentStrategy
from tracefold.trading.strategy.oi_momentum import OiMomentumConfig, OiMomentumStrategy

NOW = 1_787_000_000_000


def _bars(prices: list[tuple[int, str]]) -> list[Bar]:
    return [Bar(open_at_ms=at - 300_000, close_at_ms=at, close=Decimal(value)) for at, value in prices]


def _instrument(exchange_id: str = "binance", symbol: str = "DOGEUSDT") -> InstrumentRef:
    return InstrumentRef(
        exchange_id=exchange_id,  # type: ignore[arg-type]
        venue="binance.perp" if exchange_id == "binance" else "hl.perp",
        provider_symbol=symbol,
        base_symbol="DOGE",
        instrument_class="crypto",
        observed_at_ms=NOW,
    )


def _market(mark: str = "100", **kwargs: object) -> MarketContext:
    defaults: dict[str, object] = {
        "instrument": _instrument(),
        "mark_price": Decimal(mark),
        "observed_at_ms": NOW,
        "pre_move_bps": 200,
        "pre_move_lookback_ms": 3_600_000,
        "spread_bps": None,
        "spread_available": False,
    }
    defaults.update(kwargs)
    return MarketContext(**defaults)  # type: ignore[arg-type]


def _oi_context(
    regime: OiRegime,
    *,
    mode: str = "paper",
    whale: int = 9_900,
    value: int = 50_000_000,
    decision: TradeDecision | None = None,
    with_news: bool = False,
) -> FrozenStrategyContext:
    oi = OiTradeCandidate(
        event_id="e1",
        observed_at_ms=NOW,
        verdict_created_at_ms=NOW,
        base_symbol="DOGE",
        venue="hyperliquid",
        oi_direction="rise",
        oi_change_bps=300,
        oi_value_usd=value,
        whale_long_profit_bps=whale,
        whale_oi_ratio_bps=20_000,
        rank_in_window=1,
        final_decision="push",
        source_rule="opening_move_with_whale_concentration",
        metric_version="oi_signal_v1",
        learning_epoch="program_v7",
        program_version="news_oi_signal_v1",
        program_sha256="a" * 64,
        policy_version="news_triage_policy_v10",
        editorial_origin="telemetry_deterministic",
        editorial_sha256="b" * 64,
        scored_judgment_sha256="c" * 64,
        runtime_manifest_sha="d" * 64,
    )
    news = (
        NewsTradeCandidate(
            event_id="n1",
            verdict_created_at_ms=NOW,
            opened_at_ms=NOW,
            base_symbol="DOGE",
            evidence_version=1,
            evidence_sha256="evidence",
            focus_fact_id="f1",
            comparison_fingerprint="fp",
            source_artifact_id="x:1",
            source_published_at_ms=NOW,
            final_decision="push",
            event_type="listing",
            risk_direction="bullish",
            scope="single_name",
            magnitude=2,
            novelty="new_fact",
            headline_zh="标题",
            why_zh="机制",
            learning_epoch="program_v7",
            program_version="news_semantic_program_v5",
            program_sha256="a" * 64,
            policy_version="news_triage_policy_v10",
            editorial_origin="model",
            editorial_sha256="b" * 64,
            scored_judgment_sha256="c" * 64,
            runtime_manifest_sha="d" * 64,
        )
        if with_news
        else None
    )
    market = FrozenMarketContext(
        mark_price=Decimal("100"),
        observed_at_ms=NOW,
        pre_move_bps=250,
        pre_move_lookback_ms=3_600_000,
    )
    return FrozenStrategyContext(
        mode=mode,  # type: ignore[arg-type]
        oi=oi,
        news=news,
        regime=RegimeAssessment(regime=regime, reason="quadrant", pre_move_bps=250, oi_direction="rise"),
        market=market,
        news_decision=decision,
    )


# ---------------------------------------------------------------------------- identity
def test_canonicalisation_strips_the_provider_prefix_so_one_blacklist_row_covers_every_spelling() -> None:
    assert canonical_base_symbol("XYZ-CL") == "CL"
    assert canonical_base_symbol(" cl ") == "CL"
    assert underlying_key("XYZ-DOGE") == underlying_key("doge") == "crypto:DOGE"


def test_underlying_key_is_venue_independent() -> None:
    # The same issuer on Binance and on Hyperliquid is one bucket. This is what the cross-venue
    # single-active-order invariant is keyed on.
    assert underlying_key("SOL") == "crypto:SOL"


# ---------------------------------------------------------------------------- blacklist
def test_one_cl_row_blocks_every_provider_spelling() -> None:
    deny = Blacklist.from_rows([{"base_symbol": "CL", "reason": "commodity_not_target"}])
    assert deny.blocked("CL", now_ms=NOW) is not None
    assert deny.blocked("XYZ-CL", now_ms=NOW) is not None
    assert deny.blocked("cl", now_ms=NOW) is not None
    assert deny.blocked("SOL", now_ms=NOW) is None


def test_seeded_benchmarks_are_denied_and_an_expired_row_is_not() -> None:
    deny = Blacklist.from_rows(
        [
            {"base_symbol": "BTC", "reason": "benchmark_large_cap"},
            {"base_symbol": "ETH", "reason": "benchmark_large_cap"},
            {"base_symbol": "OLD", "reason": "temporary", "expires_at_ms": NOW - 1},
        ]
    )
    assert deny.blocked("BTC", now_ms=NOW) is not None
    assert deny.blocked("ETH", now_ms=NOW) is not None
    assert deny.blocked("OLD", now_ms=NOW) is None


def test_an_unreadable_deny_list_blocks_everything() -> None:
    # Fail closed. A deny-list that cannot be read is the one component that must never default to
    # "nothing is denied".
    deny = Blacklist.unavailable()
    blocked = deny.blocked("SOL", now_ms=NOW)
    assert blocked is not None
    assert blocked.reason == "blacklist_unavailable"


# ---------------------------------------------------------------------------- regime
@pytest.mark.parametrize("offset_ms", [0, 40_000])
def test_cutoff_history_starts_at_the_open_of_the_last_closed_bar(offset_ms: int) -> None:
    aligned_target = 1_900_000_000_000 // BAR_INTERVAL_MS * BAR_INTERVAL_MS
    target = aligned_target + offset_ms
    start = cutoff_history_start_ms(anchor_at_ms=target + 3_600_000, lookback_ms=3_600_000)
    assert start == target // BAR_INTERVAL_MS * BAR_INTERVAL_MS - BAR_INTERVAL_MS


def test_open_interest_direction_alone_is_never_a_side() -> None:
    # The reader card maps OI rise -> bullish to fit the delivery vocabulary. Trading must not inherit
    # it: with no price the answer is `unclear`, not `long`.
    assessment = assess(oi_direction="rise", move=None)
    assert assessment.regime is OiRegime.UNCLEAR
    assert assessment.reason == "no_price_fail_closed"


@pytest.mark.parametrize(
    ("direction", "move", "expected"),
    [
        ("rise", 250, OiRegime.BUILDUP_UP),
        ("rise", -250, OiRegime.BUILDUP_DOWN),
        ("fall", 250, OiRegime.DELEVERAGING_UP),
        ("fall", -250, OiRegime.DELEVERAGING_DOWN),
    ],
)
def test_the_quadrant_is_the_pair_of_open_interest_and_realised_move(
    direction: str, move: int, expected: OiRegime
) -> None:
    assert assess(oi_direction=direction, move=move).regime is expected


def test_a_move_above_the_band_is_rejected_as_chasing_not_clamped_into_it() -> None:
    # The measured loss bucket: pre-1h > 6% has a negative 4 h mean and the worst MAE. A rule with only
    # a floor would keep exactly these.
    assessment = assess(oi_direction="rise", move=1_500)
    assert assessment.regime is OiRegime.UNCLEAR
    assert assessment.reason == "move_above_band_chasing"


def test_a_move_below_the_band_is_rejected_too() -> None:
    assert assess(oi_direction="rise", move=20).reason == "move_below_band"


def test_deleveraging_never_opens() -> None:
    strategy = OiMomentumStrategy()
    for regime in (OiRegime.DELEVERAGING_UP, OiRegime.DELEVERAGING_DOWN, OiRegime.UNCLEAR):
        outcome = strategy.evaluate(_oi_context(regime))
        assert outcome.decision == "no_trade"
        assert outcome.rule.startswith("regime_no_entry")


def test_pre_move_needs_both_ends_and_never_forward_fills() -> None:
    policy = RegimePolicy(lookback_ms=3_600_000)
    bars = _bars([(NOW - 3_600_000, "100"), (NOW, "103")])
    assert pre_move_bps(bars, anchor_at_ms=NOW, policy=policy) == 300
    # A hole where the lookback start should be is missing data, not an unchanged price.
    assert pre_move_bps(_bars([(NOW, "103")]), anchor_at_ms=NOW, policy=policy) is None


# ---------------------------------------------------------------------------- policy
def _aligned(decision: str = "long", **kwargs: object) -> TradeDecision:
    defaults: dict[str, object] = {
        "decision": decision,
        "directness": "direct",
        "surprise": 3,
        "price_in": 0,
        "alignment": "aligned",
        "horizon": "hours",
        "reason_code": "none",
        "thesis_zh": "x",
        "invalidation_zh": "y",
    }
    defaults.update(kwargs)
    return TradeDecision(**defaults)  # type: ignore[arg-type]


def test_the_oi_momentum_strategy_never_reaches_a_live_mode() -> None:
    outcome = OiMomentumStrategy().evaluate(_oi_context(OiRegime.BUILDUP_UP, mode="live_reviewed"))
    assert outcome.rule == "strategy_permission_shadow_or_paper"


def test_oi_momentum_paper_decides_without_a_model() -> None:
    outcome = OiMomentumStrategy().evaluate(_oi_context(OiRegime.BUILDUP_UP))
    assert outcome.decision == "long"
    assert outcome.rule == "oi_momentum_regime"


def test_news_without_oi_is_no_trade_instead_of_a_model_chosen_side() -> None:
    context = _oi_context(OiRegime.BUILDUP_UP, decision=_aligned(), with_news=True).model_copy(update={"oi": None})
    outcome = NewsOiAlignmentStrategy().evaluate(context)
    assert outcome.rule == "oi_context_missing"


def test_a_model_that_contradicts_the_regime_is_a_no_trade() -> None:
    outcome = NewsOiAlignmentStrategy().evaluate(
        _oi_context(OiRegime.BUILDUP_UP, decision=_aligned("short"), with_news=True)
    )
    assert outcome.rule == "model_contradicts_regime"


def test_a_missing_model_answer_is_a_no_trade_for_a_news_case() -> None:
    outcome = NewsOiAlignmentStrategy().evaluate(_oi_context(OiRegime.BUILDUP_UP, decision=None, with_news=True))
    assert outcome.rule == "model_absent"


@pytest.mark.parametrize(
    ("overrides", "expected_prefix"),
    [
        ({"directness": "indirect"}, "live_requires_direct"),
        ({"surprise": 1}, "live_requires_surprise"),
        ({"price_in": 3}, "live_already_priced_in"),
        ({"alignment": "insufficient"}, "live_requires_alignment"),
    ],
)
def test_live_accepts_only_a_direct_surprising_unpriced_aligned_news_oi_case(
    overrides: dict[str, object], expected_prefix: str
) -> None:
    outcome = NewsOiAlignmentStrategy().evaluate(
        _oi_context(
            OiRegime.BUILDUP_UP,
            mode="live_reviewed",
            decision=_aligned(**overrides),
            with_news=True,
        )
    )
    assert outcome.rule.startswith(expected_prefix)


def test_a_fully_aligned_news_oi_case_is_the_only_live_acceptance() -> None:
    outcome = NewsOiAlignmentStrategy().evaluate(
        _oi_context(OiRegime.BUILDUP_UP, mode="live_reviewed", decision=_aligned(), with_news=True)
    )
    assert (outcome.decision, outcome.rule, outcome.permission) == ("long", "news_oi_aligned", "live_reviewed")


def test_shorts_are_off_by_default_and_turn_on_only_by_operator_configuration() -> None:
    context = _oi_context(OiRegime.BUILDUP_DOWN)
    assert OiMomentumStrategy().evaluate(context).rule == "short_disabled_long_only"
    enabled = OiMomentumStrategy(OiMomentumConfig(allow_short=True)).evaluate(context)
    assert enabled.decision == "short"


def test_the_measured_quality_floors_reject_the_losing_buckets() -> None:
    strategy = OiMomentumStrategy()
    weak_whale = strategy.evaluate(_oi_context(OiRegime.BUILDUP_UP, whale=8_600))
    assert weak_whale.rule == "whale_long_profit_below_floor"
    # The absolute OI floor is *not* here any more (#264). It is a universe/routability rule with one
    # owner in the Candidate Gate; a strategy re-checking it made the same number both an admission
    # rule and an Alpha rule, so moving the canary floor from 20M to 5M moved neither.
    thin = strategy.evaluate(_oi_context(OiRegime.BUILDUP_UP, value=3_000_000))
    assert thin.rule == "oi_momentum_regime"
    assert "min_oi_value_usd" not in strategy.config_snapshot


# ---------------------------------------------------------------------------- sizing
def test_sizing_is_fixed_notional_and_never_stop_distance() -> None:
    sized = size_order(side="buy", market=_market("100"), mode="paper")
    assert not isinstance(sized, RiskRejection)
    assert sized.notional_usd == Decimal("50.0000")
    assert sized.quantity == Decimal("0.5000")
    # Stop is a fixed distance from the entry, so the worst case is a multiplication, not a search.
    assert sized.stop_price == Decimal("98.00")
    assert sized.take_profit_price is None


def test_live_refuses_to_size_without_exact_precision_or_a_spread() -> None:
    unknown_step = size_order(
        side="buy",
        market=_market("100", spread_bps=5, spread_available=True),
        mode="live_bounded",
    )
    assert isinstance(unknown_step, RiskRejection)
    assert unknown_step.rule == "quantity_step_unknown"

    unknown_spread = size_order(
        side="buy",
        market=_market("100", quantity_step=Decimal("0.001")),
        mode="live_bounded",
    )
    assert isinstance(unknown_spread, RiskRejection)
    assert unknown_spread.rule == "spread_unknown_fail_closed"

    unknown_tick = size_order(
        side="buy",
        market=_market(
            "100",
            quantity_step=Decimal("0.001"),
            spread_bps=5,
            spread_available=True,
        ),
        mode="live_bounded",
    )
    assert isinstance(unknown_tick, RiskRejection)
    assert unknown_tick.rule == "price_tick_unknown"


@pytest.mark.parametrize(("side", "expected"), [("buy", "9.83"), ("sell", "10.23")])
def test_live_stop_is_rounded_to_the_provider_tick_toward_the_entry(side: str, expected: str) -> None:
    sized = size_order(
        side=side,  # type: ignore[arg-type]
        market=_market(
            "10.03",
            quantity_step=Decimal("0.1"),
            price_tick=Decimal("0.01"),
            spread_bps=5,
            spread_available=True,
        ),
        mode="live_reviewed",
    )
    assert not isinstance(sized, RiskRejection)
    assert sized.stop_price == Decimal(expected)


def test_live_stop_fails_closed_when_the_tick_would_round_it_to_the_entry() -> None:
    sized = size_order(
        side="buy",
        market=_market(
            "1",
            quantity_step=Decimal("0.1"),
            price_tick=Decimal("0.01"),
            spread_bps=5,
            spread_available=True,
        ),
        mode="live_reviewed",
        policy=OrderPolicy(fixed_stop_bps=20),
    )
    assert isinstance(sized, RiskRejection)
    assert sized.rule == "stop_price_precision_invalid"


def test_a_wide_spread_and_a_sub_minimum_quantity_both_fail_closed() -> None:
    wide = size_order(
        side="buy",
        market=_market("100", quantity_step=Decimal("0.001"), spread_bps=90, spread_available=True),
        mode="live_bounded",
    )
    assert isinstance(wide, RiskRejection) and wide.rule == "spread_above_max"

    tiny = size_order(
        side="buy",
        market=_market("100", quantity_step=Decimal("0.001"), min_quantity=Decimal("5")),
        mode="paper",
    )
    assert isinstance(tiny, RiskRejection) and tiny.rule == "quantity_below_venue_minimum"


def test_the_worst_case_daily_envelope_is_a_multiplication_the_operator_can_read() -> None:
    policy = OrderPolicy(fixed_notional_usd=Decimal("50"), fixed_stop_bps=200, max_orders_per_day=4)
    assert policy.nominal_daily_stop_loss_usd == Decimal("4.00")


def test_stop_direction_follows_the_side() -> None:
    assert stop_price_for(side="buy", entry=Decimal("100"), stop_bps=200) == Decimal("98.00")
    assert stop_price_for(side="sell", entry=Decimal("100"), stop_bps=200) == Decimal("102.00")


def test_the_frozen_payload_always_carries_hedged_and_the_venue_side_stop() -> None:
    payload = build_payload(
        instrument_exchange_id="binance",
        provider_symbol="DOGEUSDT",
        side="buy",
        quantity=Decimal("0.5"),
        stop_price=Decimal("98"),
        take_profit_price=None,
        hedged=False,
    )
    assert payload["hedged"] is False
    assert payload["stopLossPrice"] == "98"
    assert "takeProfitPrice" not in payload
    # The exact provider symbol, never a display symbol.
    assert payload["symbol"] == "DOGEUSDT"


def _protected_live_order() -> PreparedOrder:
    return PreparedOrder(
        order_id="o1",
        case_id="c1",
        underlying_key="crypto:DOGE",
        account_ref="canary",
        remote_order_id="entry-1",
        instrument=_instrument(symbol="DOGE/USDT:USDT"),
        mode="live_reviewed",
        side="buy",
        notional_usd=Decimal("10"),
        quantity=Decimal("1"),
        entry_reference=Decimal("10"),
        stop_price=Decimal("9.8"),
        take_profit_price=None,
        max_holding_ms=900_000,
        taker_fee_bps=5,
        payload={},
    )


def _protection_observation(**updates: object) -> ExecutionObservation:
    protection = NativeProtection(
        remote_order_id="stop-1",
        parent_remote_order_id="entry-1",
        account_ref="canary",
        exchange_id="binance",
        provider_symbol="DOGE/USDT:USDT",
        side="sell",
        quantity=Decimal("1"),
        trigger_price=Decimal("9.8"),
        reduce_only=True,
        status="open",
    ).model_copy(update=updates)
    return ExecutionObservation(
        state="OPEN_PROTECTED",
        observed_at_ms=NOW,
        remote_order_id="entry-1",
        actual_position_quantity=Decimal("1"),
        filled_quantity=Decimal("1"),
        average_price=Decimal("10"),
        protection=protection,
    )


@pytest.mark.parametrize(
    "mismatch",
    [
        {"parent_remote_order_id": "another-entry"},
        {"account_ref": "another-account"},
        {"exchange_id": "hyperliquid"},
        {"provider_symbol": "SOL/USDT:USDT"},
        {"side": "buy"},
        {"quantity": Decimal("0.9")},
        {"trigger_price": Decimal("9.82")},
        {"reduce_only": False},
        {"status": "cancelled"},
    ],
)
def test_native_stop_protection_requires_every_exact_provider_fact(mismatch: dict[str, object]) -> None:
    order = _protected_live_order()
    assert protection_matches(order=order, observation=_protection_observation(), price_tick=Decimal("0.01"))
    assert not protection_matches(
        order=order,
        observation=_protection_observation(**mismatch),
        price_tick=Decimal("0.01"),
    )


# ---------------------------------------------------------------------------- paper exit
def test_the_clock_closes_a_paper_position_without_news_or_a_model() -> None:
    opened = NOW
    deadline = must_close_at(opened_at_ms=opened, max_holding_ms=900_000)
    bars = _bars([(opened + 300_000, "101"), (opened + 600_000, "101"), (opened + 900_000, "101")])
    exit_at = evaluate_paper_exit(
        side="buy",
        entry=Decimal("100"),
        stop_price=Decimal("98"),
        take_profit_price=None,
        opened_at_ms=opened,
        must_close_at_ms=deadline,
        bars=bars,
        now_ms=opened + 1_000_000,
    )
    assert exit_at is not None
    assert exit_at.reason == "max_holding"


def test_the_stop_wins_over_the_clock() -> None:
    opened = NOW
    bars = _bars([(opened + 300_000, "97"), (opened + 600_000, "101")])
    exit_at = evaluate_paper_exit(
        side="buy",
        entry=Decimal("100"),
        stop_price=Decimal("98"),
        take_profit_price=None,
        opened_at_ms=opened,
        must_close_at_ms=opened + 600_000,
        bars=bars,
        now_ms=opened + 700_000,
    )
    assert exit_at is not None
    assert (exit_at.reason, exit_at.exit_price) == ("stop_loss", Decimal("97"))


def test_an_open_position_with_no_trigger_yet_stays_open() -> None:
    opened = NOW
    exit_at = evaluate_paper_exit(
        side="buy",
        entry=Decimal("100"),
        stop_price=Decimal("98"),
        take_profit_price=None,
        opened_at_ms=opened,
        must_close_at_ms=opened + 1_800_000,
        bars=_bars([(opened + 300_000, "100.5")]),
        now_ms=opened + 400_000,
    )
    assert exit_at is None


def test_both_taker_legs_are_charged_so_a_paper_receipt_is_not_flattered() -> None:
    # +100 bps gross, 5 bps each way.
    assert realized_bps(side="buy", entry=Decimal("100"), exit_price=Decimal("101"), fee_bps=5) == 90
    assert realized_bps(side="sell", entry=Decimal("100"), exit_price=Decimal("99"), fee_bps=5) == 90


# ---------------------------------------------------------------------------- schema agreement
def test_the_active_state_tuple_matches_the_partial_unique_index_predicate() -> None:
    """The index is the authority; this tuple is how the runners reason about the same set.

    They are written in two files, so without this they can drift — and the drift that matters is the
    one that silently drops a state from the index while the code still believes it is protected.
    """

    from pathlib import Path

    migration = (
        Path(__file__).resolve().parents[2]
        / "src/tracefold/platform/postgres/alembic/versions/20260823_0300_trading_core.py"
    ).read_text(encoding="utf-8")
    for state in ACTIVE_ORDER_STATES:
        assert f'"{state}"' in migration, state
    # The four that a naive reading of the spec leaves out, and which are exactly the dangerous ones.
    for state in ("APPROVED", "RECONCILING", "MANUAL_REVIEW_REQUIRED", "UNPROTECTED"):
        assert state in ACTIVE_ORDER_STATES
