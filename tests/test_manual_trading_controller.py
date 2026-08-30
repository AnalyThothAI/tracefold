"""Telegram callback flow through the App-owned News/Trading composition seam."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

import pytest

from tracefold.app.manual_trading import ManualTelegramTradingController, ManualTradingControllerConfig
from tracefold.integrations.telegram import TelegramTradingUpdate
from tracefold.trading import (
    ManualAccountSnapshot,
    ManualCloseRequest,
    ManualCloseState,
    ManualPositionState,
    ManualPositionView,
    ManualRiskConfig,
    ManualSessionState,
    ManualStrategyPresetConfig,
    ManualTargetPicker,
    ManualTargetPickerState,
    ManualTradeHistoryEvent,
    ManualTradeParameters,
    ManualTradeSession,
    ManualTradeSource,
    StrategyPreset,
    TradeSide,
)
from tracefold.trading.contracts import canonical_sha256

NOW = 1_900_000_000_000
SESSION_ID = "0198f3ae-76c0-77a1-a191-0d3f16842ea0"
CHANNEL_ID = -1001234567890
OPERATOR_ID = 123456789


def _source() -> ManualTradeSource:
    return ManualTradeSource(
        news_event_id="event-42",
        delivery_target_sha256="a" * 64,
        delivery_message_id=42,
        headline_zh="BTC ETF 净流入创纪录",
        base_symbol="BTC",
        side=TradeSide.LONG,
        source_observed_at_ms=NOW - 1_000,
    )


def _position(*, active_close: ManualCloseRequest | None = None) -> ManualPositionView:
    recommended = ManualTradeParameters(
        notional_usd=Decimal("10"),
        leverage=2,
        stop_loss_bps=100,
        take_profit_bps=200,
    )
    return ManualPositionView(
        intent_id="b" * 64,
        session_id=SESSION_ID,
        source=_source(),
        account_ref="binance-manual-live-1",
        symbol="BTCUSDT",
        side=TradeSide.LONG,
        preset=StrategyPreset.TIGHT_STOP,
        recommended=recommended,
        selected=recommended.model_copy(update={"notional_usd": Decimal("12")}),
        state=ManualPositionState.OPEN,
        quantity=Decimal("0.1"),
        entry_price=Decimal("100"),
        mark_price=Decimal("105"),
        unrealized_pnl_usd=Decimal("0.5"),
        leverage=2,
        liquidation_price=Decimal("52"),
        take_profit_price=Decimal("110"),
        stop_loss_price=Decimal("99"),
        opened_at_ms=NOW - 60_000,
        observed_at_ms=NOW,
        active_close=active_close,
    )


class _Repository:
    def __init__(self) -> None:
        self.session: ManualTradeSession | None = None
        self.picker: ManualTargetPicker | None = None
        self.reject_target_picker = False
        self.intent: Any | None = None
        self.sources = (_source(),)
        self.positions: tuple[ManualPositionView, ...] = ()
        self.events: tuple[ManualTradeHistoryEvent, ...] = ()
        self.close_requests: list[tuple[str, int, int, int]] = []

    async def sources_for_message(self, message_id: int) -> tuple[ManualTradeSource, ...]:
        return self.sources if message_id == 42 else ()

    async def begin_target_picker(
        self,
        *,
        picker_id: str,
        sources: tuple[ManualTradeSource, ...],
        actor_user_id: int,
        chat_id: int,
        now_ms: int,
    ) -> tuple[ManualTargetPicker, bool]:
        if self.reject_target_picker:
            raise ValueError("manual_target_picker_sources_conflict")
        if self.picker is not None and self.picker.state is ManualTargetPickerState.CONSUMED:
            assert self.session is not None
            if self.session.state not in {
                ManualSessionState.REJECTED,
                ManualSessionState.CANCELLED,
                ManualSessionState.CLOSED,
            }:
                raise ValueError("manual_target_picker_session_active")
            self.picker = None
        if self.picker is not None:
            return self.picker, False
        self.picker = ManualTargetPicker(
            picker_id=picker_id,
            sources_sha256=canonical_sha256([source.model_dump(mode="json") for source in sources]),
            sources=sources,
            actor_user_id=actor_user_id,
            chat_id=chat_id,
            source_message_id=sources[0].delivery_message_id,
            state=ManualTargetPickerState.PENDING,
            created_at_ms=now_ms,
            updated_at_ms=now_ms,
        )
        return self.picker, True

    async def begin_target_picker_reply(self, picker_id: str, *, now_ms: int) -> bool:
        assert self.picker is not None and self.picker.picker_id == picker_id
        if self.picker.state is not ManualTargetPickerState.PENDING:
            return False
        self.picker = self.picker.model_copy(
            update={
                "reply_attempted_at_ms": now_ms,
                "state": ManualTargetPickerState.SENDING,
                "updated_at_ms": now_ms,
            }
        )
        return True

    async def attach_target_picker_message(self, picker_id: str, *, message_id: int, now_ms: int) -> bool:
        assert self.picker is not None and self.picker.picker_id == picker_id
        self.picker = self.picker.model_copy(
            update={
                "interaction_message_id": message_id,
                "state": ManualTargetPickerState.SENT,
                "updated_at_ms": now_ms,
            }
        )
        return True

    async def get_target_picker(self, picker_id: str) -> ManualTargetPicker | None:
        return self.picker if self.picker is not None and self.picker.picker_id == picker_id else None

    async def begin_session(
        self,
        *,
        session_id: str,
        source: ManualTradeSource,
        picker_id: str | None,
        actor_user_id: int,
        chat_id: int,
        update_id: int,
        now_ms: int,
    ) -> tuple[ManualTradeSession, bool]:
        if picker_id is not None:
            assert self.picker is not None and self.picker.picker_id == picker_id
            if self.picker.state is ManualTargetPickerState.CONSUMED:
                if self.picker.selected_symbol != source.base_symbol:
                    raise ValueError("manual_target_picker_source_conflict")
                assert self.session is not None and self.picker.consumed_session_id == self.session.session_id
                return self.session, False
            assert self.picker.state is ManualTargetPickerState.SENT
        if self.session is not None:
            if self.session.source != source:
                raise ValueError("manual_trade_source_conflict")
            created = False
        else:
            self.session = ManualTradeSession(
                session_id=session_id,
                source_sha256=canonical_sha256(source.model_dump(mode="json")),
                source=source,
                actor_user_id=actor_user_id,
                chat_id=chat_id,
                source_message_id=source.delivery_message_id,
                state=ManualSessionState.AWAITING_STRATEGY,
                last_effect_update_id=update_id,
                last_effect_result_code="session_created",
                version=1,
                created_at_ms=now_ms,
                updated_at_ms=now_ms,
            )
            created = True
        if picker_id is not None:
            assert self.picker is not None and self.session is not None
            self.picker = self.picker.model_copy(
                update={
                    "selected_symbol": source.base_symbol,
                    "consumed_session_id": self.session.session_id,
                    "consumed_at_ms": now_ms,
                    "state": ManualTargetPickerState.CONSUMED,
                    "updated_at_ms": now_ms,
                }
            )
        return self.session, created

    async def begin_interaction_reply(self, session_id: str, *, now_ms: int) -> bool:
        assert self.session is not None and self.session.session_id == session_id
        if self.session.interaction_reply_attempted_at_ms is not None:
            return False
        self.session = self.session.model_copy(update={"interaction_reply_attempted_at_ms": now_ms})
        return True

    async def attach_interaction_message(self, session_id: str, *, message_id: int, now_ms: int) -> bool:
        assert self.session is not None and self.session.session_id == session_id
        self.session = self.session.model_copy(
            update={"interaction_message_id": message_id, "version": self.session.version + 1, "updated_at_ms": now_ms}
        )
        return True

    async def get_session(self, session_id: str) -> ManualTradeSession | None:
        return self.session if self.session is not None and self.session.session_id == session_id else None

    async def list_sessions(self, *, actor_user_id: int, chat_id: int) -> tuple[ManualTradeSession, ...]:
        if self.session is None:
            return ()
        assert self.session.actor_user_id == actor_user_id and self.session.chat_id == chat_id
        return (self.session,)

    async def list_positions(self, *, actor_user_id: int, chat_id: int, state: str) -> tuple[ManualPositionView, ...]:
        assert actor_user_id == OPERATOR_ID and chat_id == CHANNEL_ID
        return tuple(
            position
            for position in self.positions
            if state == "all"
            or (state == "closed" and position.state is ManualPositionState.CLOSED)
            or (state == "open" and position.state is not ManualPositionState.CLOSED)
        )

    async def get_position(
        self,
        *,
        session_id: str,
        actor_user_id: int,
        chat_id: int,
    ) -> ManualPositionView | None:
        if actor_user_id != OPERATOR_ID or chat_id != CHANNEL_ID:
            return None
        return next((position for position in self.positions if position.session_id == session_id), None)

    async def list_trade_events(
        self,
        *,
        actor_user_id: int,
        chat_id: int,
    ) -> tuple[ManualTradeHistoryEvent, ...]:
        assert actor_user_id == OPERATOR_ID and chat_id == CHANNEL_ID
        return self.events

    async def request_close(
        self,
        *,
        session_id: str,
        actor_user_id: int,
        chat_id: int,
        requested_bps: int,
        update_id: int,
        now_ms: int,
    ) -> bool:
        if (
            await self.get_position(
                session_id=session_id,
                actor_user_id=actor_user_id,
                chat_id=chat_id,
            )
            is None
        ):
            return False
        self.close_requests.append((session_id, requested_bps, update_id, now_ms))
        return True

    async def set_preview(self, **values: Any) -> ManualTradeSession | None:
        assert self.session is not None
        if self.session.last_effect_update_id == values["update_id"]:
            return self.session
        guard = values["guard"]
        state = {
            "accepted": ManualSessionState.PREVIEW,
            "high_risk_confirmation": ManualSessionState.HIGH_RISK_CONFIRMATION,
            "rejected": ManualSessionState.MODIFYING,
        }[guard.state.value]
        self.session = self.session.model_copy(
            update={
                "state": state,
                "preset": values["preset"],
                "account_snapshot": values["account_snapshot"],
                "recommended": values["recommended"],
                "selected": values["selected"],
                "preview": values["preview"],
                "guard": guard,
                "last_effect_update_id": values["update_id"],
                "last_effect_result_code": values["result_code"],
                "version": self.session.version + 1,
                "updated_at_ms": values["now_ms"],
            }
        )
        return self.session

    async def confirm_intent(
        self,
        intent: Any,
        *,
        update_id: int,
        result_code: str,
        now_ms: int,
    ) -> bool:
        assert self.session is not None
        if self.session.last_effect_update_id == update_id:
            return True
        self.intent = intent
        self.session = self.session.model_copy(
            update={
                "state": ManualSessionState.CONFIRMED,
                "intent_id": intent.intent_id,
                "last_effect_update_id": update_id,
                "last_effect_result_code": result_code,
                "version": self.session.version + 1,
                "updated_at_ms": now_ms,
            }
        )
        return True

    async def cancel_session(
        self,
        session_id: str,
        *,
        update_id: int,
        result_code: str,
        now_ms: int,
    ) -> bool:
        assert self.session is not None and self.session.session_id == session_id
        self.session = self.session.model_copy(
            update={
                "state": ManualSessionState.CANCELLED,
                "last_effect_update_id": update_id,
                "last_effect_result_code": result_code,
                "updated_at_ms": now_ms,
            }
        )
        return True


class _Bot:
    def __init__(self) -> None:
        self.answers: list[tuple[str, str, bool]] = []
        self.replies: list[tuple[int, str, tuple[tuple[str, str], ...]]] = []
        self.edits: list[tuple[int, str, tuple[tuple[str, str], ...]]] = []
        self.fail_next_edit = False

    async def answer(self, callback_query_id: str, *, text: str, show_alert: bool = False) -> None:
        self.answers.append((callback_query_id, text, show_alert))

    async def reply(self, *, source_message_id: int, text: str, keyboard: tuple[tuple[str, str], ...]) -> int:
        self.replies.append((source_message_id, text, keyboard))
        return 99

    async def edit(self, *, message_id: int, text: str, keyboard: tuple[tuple[str, str], ...]) -> None:
        if self.fail_next_edit:
            self.fail_next_edit = False
            raise RuntimeError("telegram_edit_failed")
        self.edits.append((message_id, text, keyboard))


class _SnapshotReader:
    async def snapshot_for(self, source: ManualTradeSource) -> ManualAccountSnapshot:
        assert source.base_symbol == "BTC"
        return ManualAccountSnapshot(
            account_ref="binance-manual-live-1",
            venue="binance_usdm_live",
            instrument_id="BTCUSDT",
            account_equity_usd=Decimal("1000"),
            reference_entry=Decimal("100"),
            observed_at_ms=NOW,
        )


class _UnavailableSnapshotReader:
    async def snapshot_for(self, source: ManualTradeSource) -> ManualAccountSnapshot:
        raise ValueError("manual_account_snapshot_decimal_invalid")


def _controller(
    repository: _Repository,
    bot: _Bot,
    *,
    snapshot_reader: Any | None = None,
) -> ManualTelegramTradingController:
    return ManualTelegramTradingController(
        repository=repository,
        bot=bot,
        snapshot_reader=snapshot_reader or _SnapshotReader(),
        config=ManualTradingControllerConfig(
            account_ref="binance-manual-live-1",
            venue="binance_usdm_live",
            risk=ManualRiskConfig(
                notional_deviation_limit_bps=5_000,
                tight_stop_deviation_limit_bps=5_000,
                wide_stop_deviation_limit_bps=10_000,
                max_account_risk_bps=1_000,
                high_risk_loss_multiple_bps=15_000,
                min_leverage=1,
                max_leverage=20,
            ),
            presets={
                StrategyPreset.TIGHT_STOP: ManualStrategyPresetConfig(
                    preset=StrategyPreset.TIGHT_STOP,
                    leverage=2,
                    stop_loss_bps=100,
                    take_profit_bps=200,
                    account_risk_bps=100,
                    min_notional_usd=Decimal("10"),
                    max_notional_usd=Decimal("1000"),
                ),
                StrategyPreset.WIDE_STOP: ManualStrategyPresetConfig(
                    preset=StrategyPreset.WIDE_STOP,
                    leverage=2,
                    stop_loss_bps=2_000,
                    take_profit_bps=10_000,
                    account_risk_bps=100,
                    min_notional_usd=Decimal("10"),
                    max_notional_usd=Decimal("1000"),
                ),
            },
        ),
        clock_ms=lambda: NOW,
        session_id_factory=lambda: SESSION_ID,
    )


def _update(
    data: str,
    *,
    message_id: int,
    authorized: bool = True,
    actor: int = OPERATOR_ID,
    update_id: int | None = None,
):
    stable_update_id = (
        update_id
        if update_id is not None
        else 101 + sum((index + 1) * value for index, value in enumerate(data.encode()))
    )
    return TelegramTradingUpdate(
        update_id=stable_update_id,
        callback_query_id=f"callback-{stable_update_id}-{message_id}",
        actor_user_id=actor,
        chat_id=CHANNEL_ID,
        message_id=message_id,
        data=data,
        authorized=authorized,
    )


def test_trade_button_creates_one_bound_interaction_and_strategy_selector() -> None:
    asyncio.run(_trade_button_creates_one_bound_interaction_and_strategy_selector())


async def _trade_button_creates_one_bound_interaction_and_strategy_selector() -> None:
    repository, bot = _Repository(), _Bot()
    controller = _controller(repository, bot)

    result = await controller.handle(_update("tf:trade:v1", message_id=42))

    assert result == "session_created"
    assert repository.session is not None
    assert repository.session.interaction_message_id == 99
    assert bot.replies[0][0] == 42
    assert "BTC ETF 净流入创纪录" in bot.replies[0][1]
    assert bot.replies[0][2] == (
        ("大仓位 / 小止损", f"tf:p:t:{SESSION_ID}"),
        ("小仓位 / 大止损", f"tf:p:w:{SESSION_ID}"),
        ("取消", f"tf:x:{SESSION_ID}"),
    )


def test_trade_button_requires_target_selection_when_the_card_displays_multiple_assets() -> None:
    repository, bot = _Repository(), _Bot()
    repository.sources = (
        _source().model_copy(update={"base_symbol": "HYPE", "headline_zh": "一条多标的新闻"}),
        _source().model_copy(update={"base_symbol": "ETH", "headline_zh": "一条多标的新闻"}),
    )
    controller = _controller(repository, bot)

    result = asyncio.run(controller.handle(_update("tf:trade:v1", message_id=42)))

    assert result == "target_picker_created"
    assert repository.session is None
    assert bot.replies == [
        (
            42,
            "🎯 <b>选择交易标的</b>\n\n这条 TG 新闻展示了多个标的，请先选择本次要交易的标的。",
            (("HYPE", f"tf:t:HYPE:{SESSION_ID}"), ("ETH", f"tf:t:ETH:{SESSION_ID}")),
        )
    ]

    assert asyncio.run(controller.handle(_update("tf:trade:v1", message_id=42))) == "target_picker_resumed"
    assert len(bot.replies) == 1

    selected = asyncio.run(controller.handle(_update(f"tf:t:ETH:{SESSION_ID}", message_id=99)))

    assert selected == "session_created"
    assert repository.session is not None
    assert repository.session.source.base_symbol == "ETH"
    assert repository.session.interaction_message_id == 99
    assert "<b>ETH 做多</b>" in bot.edits[-1][1]
    assert bot.edits[-1][2] == (
        ("大仓位 / 小止损", f"tf:p:t:{SESSION_ID}"),
        ("小仓位 / 大止损", f"tf:p:w:{SESSION_ID}"),
        ("取消", f"tf:x:{SESSION_ID}"),
    )
    assert asyncio.run(controller.handle(_update("tf:trade:v1", message_id=42))) == "target_session_active"
    assert len(bot.replies) == 1


def test_target_callback_cannot_select_prose_symbol_or_replace_an_existing_choice() -> None:
    repository, bot = _Repository(), _Bot()
    repository.sources = (
        _source().model_copy(update={"base_symbol": "HYPE"}),
        _source().model_copy(update={"base_symbol": "ETH"}),
    )
    controller = _controller(repository, bot)

    assert (
        asyncio.run(controller.handle(_update(f"tf:t:HYPE:{SESSION_ID}", message_id=99)))
        == "target_picker_binding_mismatch"
    )
    assert repository.session is None

    assert asyncio.run(controller.handle(_update("tf:trade:v1", message_id=42))) == "target_picker_created"
    assert asyncio.run(controller.handle(_update(f"tf:t:ZEC:{SESSION_ID}", message_id=99))) == "target_invalid"
    assert repository.session is None
    assert bot.answers[-1][2] is True

    assert asyncio.run(controller.handle(_update(f"tf:t:HYPE:{SESSION_ID}", message_id=99))) == "session_created"
    assert repository.session is not None and repository.session.source.base_symbol == "HYPE"

    assert asyncio.run(controller.handle(_update(f"tf:t:ETH:{SESSION_ID}", message_id=99))) == "source_conflict"
    assert repository.session.source.base_symbol == "HYPE"
    assert bot.answers[-1][2] is True


def test_trade_button_fails_closed_when_displayed_targets_changed_since_picker_creation() -> None:
    repository, bot = _Repository(), _Bot()
    repository.sources = (
        _source().model_copy(update={"base_symbol": "HYPE"}),
        _source().model_copy(update={"base_symbol": "ETH"}),
    )
    repository.reject_target_picker = True
    controller = _controller(repository, bot)

    result = asyncio.run(controller.handle(_update("tf:trade:v1", message_id=42)))

    assert result == "target_picker_stale"
    assert repository.session is None
    assert bot.replies == []
    assert bot.answers[-1][2] is True


def test_target_selection_replays_the_strategy_edit_after_a_post_attach_failure() -> None:
    repository, bot = _Repository(), _Bot()
    repository.sources = (
        _source().model_copy(update={"base_symbol": "HYPE"}),
        _source().model_copy(update={"base_symbol": "ETH"}),
    )
    controller = _controller(repository, bot)
    asyncio.run(controller.handle(_update("tf:trade:v1", message_id=42)))
    target_update = _update(f"tf:t:HYPE:{SESSION_ID}", message_id=99)
    bot.fail_next_edit = True

    with pytest.raises(RuntimeError, match="telegram_edit_failed"):
        asyncio.run(controller.handle(target_update))

    assert repository.session is not None
    assert repository.session.interaction_message_id == 99
    repository.sources = (
        _source().model_copy(update={"base_symbol": "SOL"}),
        _source().model_copy(update={"base_symbol": "ETH"}),
    )
    assert asyncio.run(controller.handle(target_update)) == "session_resumed"
    assert "<b>HYPE 做多</b>" in bot.edits[-1][1]


def test_modifications_recompute_combined_risk_and_require_distinct_high_risk_confirmation() -> None:
    asyncio.run(_modifications_recompute_combined_risk_and_require_distinct_high_risk_confirmation())


def test_snapshot_outage_is_rendered_in_the_interaction_with_retry_controls() -> None:
    repository, bot = _Repository(), _Bot()
    controller = _controller(repository, bot, snapshot_reader=_UnavailableSnapshotReader())
    asyncio.run(controller.handle(_update("tf:trade:v1", message_id=42)))

    result = asyncio.run(controller.handle(_update(f"tf:p:t:{SESSION_ID}", message_id=99)))

    assert result == "account_snapshot_unavailable"
    assert "账户或行情快照暂不可用" in bot.edits[-1][1]
    assert bot.edits[-1][2] == (
        ("大仓位 / 小止损", f"tf:p:t:{SESSION_ID}"),
        ("小仓位 / 大止损", f"tf:p:w:{SESSION_ID}"),
        ("取消", f"tf:x:{SESSION_ID}"),
    )


async def _modifications_recompute_combined_risk_and_require_distinct_high_risk_confirmation() -> None:
    repository, bot = _Repository(), _Bot()
    controller = _controller(repository, bot)
    await controller.handle(_update("tf:trade:v1", message_id=42))

    assert await controller.handle(_update(f"tf:p:t:{SESSION_ID}", message_id=99)) == "preview_ready"
    assert repository.session is not None
    assert repository.session.state is ManualSessionState.PREVIEW
    preview_text = bot.edits[-1][1]
    assert "<b>BTC 做多</b>" in preview_text
    assert "交易市场：币安 U 本位合约（正式站）" in preview_text
    assert "账户权益：$1000.00" in preview_text
    assert "委托类型：市价" in preview_text
    assert "预计亏损：-$10.00" in preview_text
    assert "账户风险：-1.00%" in preview_text
    assert not any(
        label in preview_text for label in ("Venue:", "Account Equity:", "Strategy:", "Order:", "Estimated Loss:")
    )
    assert ("修改", f"tf:m:{SESSION_ID}") in bot.edits[-1][2]

    assert await controller.handle(_update(f"tf:a:n:u:{SESSION_ID}", message_id=99)) == "modification_applied"
    assert await controller.handle(_update(f"tf:a:s:u:{SESSION_ID}", message_id=99)) == "modification_applied"
    assert repository.session is not None
    assert repository.session.state is ManualSessionState.HIGH_RISK_CONFIRMATION
    assert repository.session.guard is not None
    assert repository.session.guard.modified_max_loss_usd == Decimal("22.50")
    assert "原最大亏损 $10.00 → 新最大亏损 $22.50" in bot.edits[-1][1]
    assert ("仍然执行", f"tf:h:{SESSION_ID}") in bot.edits[-1][2]

    assert await controller.handle(_update(f"tf:h:{SESSION_ID}", message_id=99)) == "intent_confirmed"
    assert repository.intent is not None
    assert repository.intent.high_risk_confirmed_at_ms == NOW
    assert repository.intent.guard.state.value == "high_risk_confirmation"


def test_replayed_adjustment_renders_the_committed_effect_without_applying_it_twice() -> None:
    repository, bot = _Repository(), _Bot()
    controller = _controller(repository, bot)
    asyncio.run(controller.handle(_update("tf:trade:v1", message_id=42)))
    asyncio.run(controller.handle(_update(f"tf:p:t:{SESSION_ID}", message_id=99)))
    adjustment = _update(f"tf:a:n:u:{SESSION_ID}", message_id=99, update_id=9_000_000)

    assert asyncio.run(controller.handle(adjustment)) == "modification_applied"
    assert repository.session is not None
    once = repository.session.selected
    assert once is not None and once.notional_usd == Decimal("1500.00")

    assert asyncio.run(controller.handle(adjustment)) == "modification_applied"
    assert repository.session is not None and repository.session.selected == once


def test_unauthorized_or_forged_session_callbacks_never_reach_trading_state() -> None:
    asyncio.run(_unauthorized_or_forged_session_callbacks_never_reach_trading_state())


async def _unauthorized_or_forged_session_callbacks_never_reach_trading_state() -> None:
    repository, bot = _Repository(), _Bot()
    controller = _controller(repository, bot)

    assert await controller.handle(_update("tf:trade:v1", message_id=42, authorized=False, actor=777)) == "unauthorized"
    assert repository.session is None
    assert bot.answers[-1][2] is True

    await controller.handle(_update("tf:trade:v1", message_id=42))
    assert await controller.handle(_update(f"tf:p:t:{SESSION_ID}", message_id=777)) == "session_binding_mismatch"
    assert repository.session is not None
    assert repository.session.state is ManualSessionState.AWAITING_STRATEGY


def test_my_trades_opens_the_private_portfolio_menu() -> None:
    repository, bot = _Repository(), _Bot()
    controller = _controller(repository, bot)
    asyncio.run(controller.handle(_update("tf:trade:v1", message_id=42)))

    result = asyncio.run(controller.handle(_update("tf:mine:v1", message_id=99)))

    assert result == "portfolio_menu_shown"
    assert "我的交易" in bot.edits[-1][1]
    assert bot.edits[-1][2] == (
        ("当前持仓", "tf:mine:open"),
        ("历史持仓", "tf:mine:closed"),
        ("交易记录", "tf:mine:events"),
    )


def test_detail_keeps_the_enriched_news_message_in_place_without_sending_another_card() -> None:
    repository, bot = _Repository(), _Bot()
    controller = _controller(repository, bot)

    result = asyncio.run(controller.handle(_update("tf:detail:v1", message_id=42)))

    assert result == "detail_shown"
    assert bot.replies == []
    assert bot.edits == []
    assert bot.answers[-1][2] is True
    assert "已在原新闻中原位补全" in bot.answers[-1][1]


def test_position_detail_exposes_complete_chinese_data_and_real_close_confirmation() -> None:
    repository, bot = _Repository(), _Bot()
    repository.positions = (_position(),)
    controller = _controller(repository, bot)

    assert asyncio.run(controller.handle(_update(f"tf:pos:{SESSION_ID}", message_id=99))) == "position_shown"
    detail = bot.edits[-1][1]
    for expected in (
        "BTC 持仓详情",
        "当前名义价值：$10.50",
        "占用保证金估算：$5.25",
        "未实现盈亏：+0.5000 USDT",
        "持仓收益率：+5.00%",
        "保证金回报率：+10.00%",
        "原推荐：10.00U / 2x",
        "最终参数：12.00U / 2x",
        "关联新闻：BTC ETF 净流入创纪录",
    ):
        assert expected in detail
    assert ("查看原新闻", f"tf:news:{SESSION_ID}") in bot.edits[-1][2]
    assert ("平仓", f"tf:close:{SESSION_ID}") in bot.edits[-1][2]

    assert asyncio.run(controller.handle(_update(f"tf:news:{SESSION_ID}", message_id=99))) == "position_news_linked"
    assert bot.replies[-1][0] == 42

    assert asyncio.run(controller.handle(_update(f"tf:close:{SESSION_ID}", message_id=99))) == "position_close_opened"
    assert asyncio.run(controller.handle(_update(f"tf:closep:5000:{SESSION_ID}", message_id=99))) == (
        "position_close_preview"
    )
    assert "确认真实平仓" in bot.edits[-1][1]
    assert "reduce-only 市价单" in bot.edits[-1][1]
    assert asyncio.run(controller.handle(_update(f"tf:closec:5000:{SESSION_ID}", message_id=99))) == (
        "position_close_requested"
    )
    assert repository.close_requests[-1][0:2] == (SESSION_ID, 5000)


def test_filled_partial_close_keeps_the_next_close_button_available() -> None:
    filled = ManualCloseRequest(
        close_id="c" * 64,
        intent_id="b" * 64,
        session_id=SESSION_ID,
        requested_bps=3000,
        client_order_id="tfm-c-example",
        state=ManualCloseState.FILLED,
        target_quantity=Decimal("0.03"),
        attempted_at_ms=NOW - 2_000,
        receipt={"status": "FILLED"},
        reconciled_at_ms=NOW,
        requested_at_ms=NOW - 3_000,
        updated_at_ms=NOW - 1_000,
    )
    repository, bot = _Repository(), _Bot()
    repository.positions = (_position(active_close=filled),)
    controller = _controller(repository, bot)

    assert asyncio.run(controller.handle(_update(f"tf:pos:{SESSION_ID}", message_id=99))) == "position_shown"
    assert ("平仓", f"tf:close:{SESSION_ID}") in bot.edits[-1][2]
