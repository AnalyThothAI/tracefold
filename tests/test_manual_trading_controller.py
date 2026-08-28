"""Telegram callback flow through the App-owned News/Trading composition seam."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

from tracefold.app.manual_trading import ManualTelegramTradingController, ManualTradingControllerConfig
from tracefold.integrations.telegram import TelegramTradingUpdate
from tracefold.trading import (
    ManualAccountSnapshot,
    ManualRiskConfig,
    ManualSessionState,
    ManualStrategyPresetConfig,
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


class _Repository:
    def __init__(self) -> None:
        self.session: ManualTradeSession | None = None
        self.intent: Any | None = None

    async def source_for_message(self, message_id: int) -> ManualTradeSource | None:
        return _source() if message_id == 42 else None

    async def begin_session(
        self,
        *,
        session_id: str,
        source: ManualTradeSource,
        actor_user_id: int,
        chat_id: int,
        update_id: int,
        now_ms: int,
    ) -> tuple[ManualTradeSession, bool]:
        if self.session is not None:
            return self.session, False
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
        return self.session, True

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

    async def answer(self, callback_query_id: str, *, text: str, show_alert: bool = False) -> None:
        self.answers.append((callback_query_id, text, show_alert))

    async def reply(self, *, source_message_id: int, text: str, keyboard: tuple[tuple[str, str], ...]) -> int:
        self.replies.append((source_message_id, text, keyboard))
        return 99

    async def edit(self, *, message_id: int, text: str, keyboard: tuple[tuple[str, str], ...]) -> None:
        self.edits.append((message_id, text, keyboard))


class _SnapshotReader:
    async def snapshot_for(self, source: ManualTradeSource) -> ManualAccountSnapshot:
        assert source.base_symbol == "BTC"
        return ManualAccountSnapshot(
            account_ref="binance-manual-demo-1",
            venue="binance_usdm_demo",
            instrument_id="BTCUSDT",
            account_equity_usd=Decimal("1000"),
            reference_entry=Decimal("100"),
            observed_at_ms=NOW,
        )


def _controller(repository: _Repository, bot: _Bot) -> ManualTelegramTradingController:
    return ManualTelegramTradingController(
        repository=repository,
        bot=bot,
        snapshot_reader=_SnapshotReader(),
        config=ManualTradingControllerConfig(
            account_ref="binance-manual-demo-1",
            venue="binance_usdm_demo",
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


def test_modifications_recompute_combined_risk_and_require_distinct_high_risk_confirmation() -> None:
    asyncio.run(_modifications_recompute_combined_risk_and_require_distinct_high_risk_confirmation())


async def _modifications_recompute_combined_risk_and_require_distinct_high_risk_confirmation() -> None:
    repository, bot = _Repository(), _Bot()
    controller = _controller(repository, bot)
    await controller.handle(_update("tf:trade:v1", message_id=42))

    assert await controller.handle(_update(f"tf:p:t:{SESSION_ID}", message_id=99)) == "preview_ready"
    assert repository.session is not None
    assert repository.session.state is ManualSessionState.PREVIEW
    assert "Estimated Loss: -$10.00" in bot.edits[-1][1]
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


def test_my_trades_refreshes_the_bound_interaction_with_durable_state() -> None:
    repository, bot = _Repository(), _Bot()
    controller = _controller(repository, bot)
    asyncio.run(controller.handle(_update("tf:trade:v1", message_id=42)))

    result = asyncio.run(controller.handle(_update("tf:mine:v1", message_id=99)))

    assert result == "my_trades_shown"
    assert "BTC LONG — 待选策略" in bot.edits[-1][1]
    assert bot.edits[-1][2] == (("刷新", "tf:mine:v1"),)


def test_detail_keeps_the_enriched_news_message_in_place_without_sending_another_card() -> None:
    repository, bot = _Repository(), _Bot()
    controller = _controller(repository, bot)

    result = asyncio.run(controller.handle(_update("tf:detail:v1", message_id=42)))

    assert result == "detail_shown"
    assert bot.replies == []
    assert bot.edits == []
    assert bot.answers[-1][2] is True
    assert "已在原新闻中原位补全" in bot.answers[-1][1]
