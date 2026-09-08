"""Buy notification decisions and reader facts share the public market seam (#614)."""

from __future__ import annotations

from dataclasses import replace

import pytest

from tracefold.news.market_notifications import MarketObservation, decide_group, group_identity, market_reader_card


def _buy(**overrides) -> MarketObservation:
    return replace(
        MarketObservation(
            item_id="buy-1",
            market_kind="wallet",
            parse_status="parsed",
            title="Alice buys TOKEN",
            event_at_ms=1_000_000,
            received_at_ms=1_000_100,
            provider="robinhood_chain",
            wallet_kind="buy",
            wallet_address="0x111",
            wallet_handle="Alice",
            wallet_token="0xaaa",
            wallet_segment_key="window-1",
            wallet_notify_eligible=True,
            wallet_stage="first_observed",
            wallet_selection_reason="selected",
            wallet_buy_count=2,
            wallet_unpriced_buys=1,
            wallet_usd="1000",
            wallet_entry_price="10",
            wallet_mark_price="12",
            wallet_observed_at_ms=1_000_100,
            wallet_window_from_ms=900_000,
            symbol="TOKEN",
        ),
        **overrides,
    )


@pytest.mark.parametrize("kind", ["buy", "exit"])
def test_candidates_without_explicit_notification_eligibility_do_not_open_intents(kind: str) -> None:
    observation = _buy(wallet_kind=kind, wallet_notify_eligible=False, wallet_selection_reason="below_minimum")
    identity = group_identity(observation)

    result = decide_group(None, identity, (observation,), now_ms=1_000_100, has_open_intent=False)

    assert result.intent is None
    assert result.track.last_observed_item_id == observation.item_id
    assert result.track.pending_reason == "below_minimum"


def test_first_eligible_buy_triggers_and_different_wallets_never_share_a_group() -> None:
    suppressed = _buy(item_id="buy-small", wallet_notify_eligible=False)
    eligible = _buy(item_id="buy-large", received_at_ms=1_000_200)
    identity = group_identity(eligible)

    result = decide_group(None, identity, (suppressed, eligible), now_ms=1_000_300, has_open_intent=False)

    assert result.intent is not None and result.intent.trigger_item_id == "buy-large"
    assert result.track.round_started_at_ms == 1_000_200
    assert group_identity(_buy(wallet_address="0x222")).group_key != identity.group_key


def test_buy_card_states_priced_amount_stage_and_observation_price_without_exit_language() -> None:
    observation = _buy()
    card = market_reader_card(track=group_identity(observation), reason="first", observations=(observation,))
    body = "\n".join(card.body_lines())

    assert card.title() == "链上钱包 · 买入观察 · TOKEN"
    assert "Alice" in body and "代币 0xaaa" in body
    assert "窗口买入 2 笔" in body and "未计价 1 笔" in body
    assert "已计价金额 $1,000.00" in body
    assert "已计价部分均价 $10" in body and "观察价 $12" in body
    assert "首次观察买入（此前持仓未知）" in body
    assert "减仓" not in body and "清仓" not in body
