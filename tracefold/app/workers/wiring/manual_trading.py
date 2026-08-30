"""App composition for Telegram-first manual Trading.

News and Trading remain sibling capabilities.  This module is the only place that projects a delivered
News event into the public manual-Trading source contract and later wires that contract to Telegram.
"""

from __future__ import annotations

import asyncio
import html
import time
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from decimal import Decimal
from functools import partial
from typing import Any, Literal, Protocol, cast

from loguru import logger

from tracefold.app.manual_trading import ManualTelegramTradingController, ManualTradingControllerConfig
from tracefold.app.telegram_test_news import TelegramDevelopmentTestNewsController
from tracefold.app.worker_database import WorkerDatabase
from tracefold.app.workers.capabilities import FiniteOperations
from tracefold.app.workers.wiring.database import WorkerTradingDatabase
from tracefold.integrations.telegram import TelegramTradingClient, TelegramTradingUpdate
from tracefold.integrations.venues.quotes import fetch_binance_futures_quotes
from tracefold.news import TelegramManualTradeProjectionV1
from tracefold.platform.config.models import (
    Settings,
    manual_trading_availability,
    manual_trading_profile_availability,
    onchain_trading_availability,
)
from tracefold.platform.config.secret_file import SecretFileError, read_secure_secret_text
from tracefold.trading import (
    ManualAccountSnapshot,
    ManualModificationGuard,
    ManualPositionView,
    ManualRiskConfig,
    ManualStrategyPresetConfig,
    ManualTargetPicker,
    ManualTradeHistoryEvent,
    ManualTradeIntent,
    ManualTradeParameters,
    ManualTradePreview,
    ManualTradeSession,
    ManualTradeSource,
    StrategyPreset,
    TradeSide,
)

_MANUAL_TELEGRAM_POLL_SECONDS = 1.0


def manual_trade_sources_from_news_projection(
    projection: TelegramManualTradeProjectionV1,
    *,
    message_id: int,
    target_sha256: str,
) -> tuple[ManualTradeSource, ...]:
    """Map exactly the target symbols rendered on the delivered Telegram card."""

    if projection.projection_version != "telegram_manual_trade_projection_v1":
        return ()
    if projection.final_decision not in {"push", "escalate"} or projection.degraded:
        return ()
    side = {"bullish": TradeSide.LONG, "bearish": TradeSide.SHORT}.get(projection.direction)
    if side is None:
        return ()
    try:
        return tuple(
            ManualTradeSource(
                news_event_id=projection.event_id,
                delivery_target_sha256=target_sha256,
                delivery_message_id=message_id,
                headline_zh=projection.title_zh,
                base_symbol=symbol,
                side=side,
                source_observed_at_ms=projection.opened_at_ms,
            )
            for symbol in projection.displayed_assets
        )
    except (TypeError, ValueError):
        return ()


def manual_trade_sources_from_development_test_news(
    row: Mapping[str, Any],
    *,
    message_id: int,
    target_sha256: str,
) -> tuple[ManualTradeSource, ...]:
    """Map only an explicitly created, unexpired futures test fixture."""

    if row.get("test_kind") != "futures" or row.get("delivery_target_sha256") != target_sha256:
        return ()
    side = {"bullish": TradeSide.LONG, "bearish": TradeSide.SHORT}.get(str(row.get("direction") or ""))
    targets = row.get("displayed_targets")
    if side is None or not isinstance(targets, list | tuple):
        return ()
    try:
        return tuple(
            ManualTradeSource(
                news_event_id=f"development-test:{row['source_id']}",
                delivery_target_sha256=target_sha256,
                delivery_message_id=message_id,
                headline_zh=str(row["headline_zh"]),
                base_symbol=str(symbol),
                side=side,
                source_observed_at_ms=int(row["source_observed_at_ms"]),
            )
            for symbol in targets
        )
    except (KeyError, TypeError, ValueError):
        return ()


class ManualTradingRepositoryAdapter:
    """App-owned adapter spanning the News read projection and Trading write ledger."""

    def __init__(self, database: WorkerTradingDatabase, *, target_sha256: str) -> None:
        self._database = database
        self._target_sha256 = target_sha256

    async def sources_for_message(self, message_id: int) -> tuple[ManualTradeSource, ...]:
        def read(repos: Any) -> tuple[ManualTradeSource, ...]:
            projection = repos.news.telegram_manual_trade_projection(
                message_id=message_id,
                target_sha256=self._target_sha256,
            )
            if projection is not None:
                return manual_trade_sources_from_news_projection(
                    projection,
                    message_id=message_id,
                    target_sha256=self._target_sha256,
                )
            read_test = getattr(repos.trading, "telegram_development_test_news", None)
            if not callable(read_test):
                return ()
            test_row = read_test(
                message_id=message_id,
                target_sha256=self._target_sha256,
                now_ms=time.time_ns() // 1_000_000,
            )
            if not isinstance(test_row, Mapping):
                return ()
            return manual_trade_sources_from_development_test_news(
                test_row,
                message_id=message_id,
                target_sha256=self._target_sha256,
            )

        return await self._database.read("manual_trading_source", read, timeout_seconds=3.0)

    async def begin_target_picker(
        self,
        *,
        picker_id: str,
        sources: tuple[ManualTradeSource, ...],
        actor_user_id: int,
        chat_id: int,
        now_ms: int,
    ) -> tuple[ManualTargetPicker, bool]:
        return await self._database.tx(
            "manual_trading_begin_target_picker",
            lambda repos: repos.trading.begin_manual_target_picker(
                picker_id=picker_id,
                sources=sources,
                actor_user_id=actor_user_id,
                chat_id=chat_id,
                now_ms=now_ms,
            ),
            timeout_seconds=3.0,
        )

    async def begin_target_picker_reply(self, picker_id: str, *, now_ms: int) -> bool:
        return await self._database.tx(
            "manual_trading_begin_target_picker_reply",
            lambda repos: repos.trading.begin_manual_target_picker_reply(picker_id, now_ms=now_ms),
            timeout_seconds=3.0,
        )

    async def attach_target_picker_message(self, picker_id: str, *, message_id: int, now_ms: int) -> bool:
        return await self._database.tx(
            "manual_trading_attach_target_picker",
            lambda repos: repos.trading.attach_manual_target_picker_message(
                picker_id,
                message_id=message_id,
                now_ms=now_ms,
            ),
            timeout_seconds=3.0,
        )

    async def get_target_picker(self, picker_id: str) -> ManualTargetPicker | None:
        return await self._database.read(
            "manual_trading_get_target_picker",
            lambda repos: repos.trading.manual_target_picker(picker_id),
            timeout_seconds=3.0,
        )

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
        def begin(repos: Any) -> tuple[ManualTradeSession, bool]:
            if picker_id is not None:
                return cast(
                    tuple[ManualTradeSession, bool],
                    repos.trading.begin_manual_trade_session_from_picker(
                        session_id=session_id,
                        picker_id=picker_id,
                        source=source,
                        actor_user_id=actor_user_id,
                        chat_id=chat_id,
                        update_id=update_id,
                        now_ms=now_ms,
                    ),
                )
            return cast(
                tuple[ManualTradeSession, bool],
                repos.trading.begin_manual_trade_session(
                    session_id=session_id,
                    source=source,
                    actor_user_id=actor_user_id,
                    chat_id=chat_id,
                    update_id=update_id,
                    now_ms=now_ms,
                ),
            )

        return await self._database.tx(
            "manual_trading_begin_session",
            begin,
            timeout_seconds=3.0,
        )

    async def begin_interaction_reply(self, session_id: str, *, now_ms: int) -> bool:
        return await self._database.tx(
            "manual_trading_begin_interaction_reply",
            lambda repos: repos.trading.begin_manual_interaction_reply(session_id, now_ms=now_ms),
            timeout_seconds=3.0,
        )

    async def attach_interaction_message(self, session_id: str, *, message_id: int, now_ms: int) -> bool:
        return await self._database.tx(
            "manual_trading_attach_interaction",
            lambda repos: repos.trading.attach_manual_interaction_message(
                session_id,
                message_id=message_id,
                now_ms=now_ms,
            ),
            timeout_seconds=3.0,
        )

    async def get_session(self, session_id: str) -> ManualTradeSession | None:
        return await self._database.read(
            "manual_trading_get_session",
            lambda repos: repos.trading.manual_trade_session(session_id),
            timeout_seconds=3.0,
        )

    async def list_sessions(self, *, actor_user_id: int, chat_id: int) -> tuple[ManualTradeSession, ...]:
        return await self._database.read(
            "manual_trading_list_sessions",
            lambda repos: tuple(
                repos.trading.manual_trade_sessions_for_actor(
                    actor_user_id=actor_user_id,
                    chat_id=chat_id,
                )
            ),
            timeout_seconds=3.0,
        )

    async def list_positions(
        self,
        *,
        actor_user_id: int,
        chat_id: int,
        state: str,
    ) -> tuple[ManualPositionView, ...]:
        return await self._database.read(
            "manual_trading_list_positions",
            lambda repos: tuple(
                repos.trading.manual_positions_for_actor(
                    actor_user_id=actor_user_id,
                    chat_id=chat_id,
                    state=cast(Literal["open", "closed", "all"], state),
                )
            ),
            timeout_seconds=3.0,
        )

    async def get_position(
        self,
        *,
        session_id: str,
        actor_user_id: int,
        chat_id: int,
    ) -> ManualPositionView | None:
        return await self._database.read(
            "manual_trading_get_position",
            lambda repos: repos.trading.manual_position_for_actor(
                session_id=session_id,
                actor_user_id=actor_user_id,
                chat_id=chat_id,
            ),
            timeout_seconds=3.0,
        )

    async def list_trade_events(
        self,
        *,
        actor_user_id: int,
        chat_id: int,
    ) -> tuple[ManualTradeHistoryEvent, ...]:
        return await self._database.read(
            "manual_trading_list_events",
            lambda repos: tuple(
                repos.trading.manual_trade_history_for_actor(
                    actor_user_id=actor_user_id,
                    chat_id=chat_id,
                )
            ),
            timeout_seconds=3.0,
        )

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
        row = await self._database.tx(
            "manual_trading_request_close",
            lambda repos: repos.trading.request_manual_position_close(
                session_id=session_id,
                actor_user_id=actor_user_id,
                chat_id=chat_id,
                requested_bps=cast(Literal[3000, 5000, 10000], requested_bps),
                update_id=update_id,
                now_ms=now_ms,
            ),
            timeout_seconds=3.0,
        )
        return row is not None

    async def set_preview(
        self,
        *,
        session_id: str,
        preset: StrategyPreset,
        account_snapshot: ManualAccountSnapshot,
        recommended: ManualTradeParameters,
        selected: ManualTradeParameters,
        preview: ManualTradePreview,
        guard: ManualModificationGuard,
        update_id: int,
        result_code: str,
        now_ms: int,
    ) -> ManualTradeSession | None:
        return await self._database.tx(
            "manual_trading_set_preview",
            lambda repos: repos.trading.set_manual_trade_preview(
                session_id=session_id,
                preset=preset,
                account_snapshot=account_snapshot,
                recommended=recommended,
                selected=selected,
                preview=preview,
                guard=guard,
                update_id=update_id,
                result_code=result_code,
                now_ms=now_ms,
            ),
            timeout_seconds=3.0,
        )

    async def confirm_intent(
        self,
        intent: ManualTradeIntent,
        *,
        update_id: int,
        result_code: str,
        now_ms: int,
    ) -> bool:
        return await self._database.tx(
            "manual_trading_confirm_intent",
            lambda repos: repos.trading.confirm_manual_trade_intent(
                intent,
                update_id=update_id,
                result_code=result_code,
                now_ms=now_ms,
            ),
            timeout_seconds=3.0,
        )

    async def cancel_session(
        self,
        session_id: str,
        *,
        update_id: int,
        result_code: str,
        now_ms: int,
    ) -> bool:
        return await self._database.tx(
            "manual_trading_cancel_session",
            lambda repos: repos.trading.cancel_manual_trade_session(
                session_id,
                update_id=update_id,
                result_code=result_code,
                now_ms=now_ms,
            ),
            timeout_seconds=3.0,
        )


class ManualAccountMarketSnapshotReader:
    """Combine executor-owned equity truth with a current credential-free USD-M reference price."""

    def __init__(
        self,
        database: WorkerTradingDatabase,
        *,
        account_ref: str,
        clock_ms: Any | None = None,
        quote_fetcher: Any = fetch_binance_futures_quotes,
        max_equity_age_ms: int = 30_000,
        max_quote_age_ms: int = 15_000,
    ) -> None:
        self._database = database
        self._account_ref = account_ref
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._quote_fetcher = quote_fetcher
        self._max_equity_age_ms = max_equity_age_ms
        self._max_quote_age_ms = max_quote_age_ms

    async def snapshot_for(self, source: ManualTradeSource) -> ManualAccountSnapshot:
        account = await self._database.read(
            "manual_trading_account_snapshot",
            lambda repos: repos.trading.manual_account_snapshot(self._account_ref),
            timeout_seconds=3.0,
        )
        now_ms = int(self._clock_ms())
        if account is None or not _fresh_timestamp(account.get("observed_at_ms"), now_ms, self._max_equity_age_ms):
            raise RuntimeError("manual_trading_account_snapshot_stale")
        instrument_id = f"{source.base_symbol}USDT"
        try:
            quotes = await self._quote_fetcher((instrument_id,))
        except Exception as exc:
            raise RuntimeError("manual_trading_market_quote_unavailable") from exc
        quote = next((value for value in quotes if value.venue_symbol == instrument_id), None)
        if quote is None:
            raise RuntimeError("manual_trading_market_quote_unavailable")
        quote_at_ms = quote.source_at_ms if quote.source_at_ms is not None else now_ms
        if not _fresh_timestamp(quote_at_ms, now_ms, self._max_quote_age_ms):
            raise RuntimeError("manual_trading_market_quote_stale")
        return ManualAccountSnapshot(
            account_ref=self._account_ref,
            venue=str(account["venue"]),
            instrument_id=instrument_id,
            account_equity_usd=Decimal(str(account["equity_usd"])),
            reference_entry=quote.price,
            observed_at_ms=min(int(account["observed_at_ms"]), int(quote_at_ms)),
        )


class ManualTradingBotAdapter:
    def __init__(self, client: TelegramTradingClient, finite: FiniteOperations, *, chat_id: int) -> None:
        self._client = client
        self._finite = finite
        self._chat_id = chat_id

    async def answer(self, callback_query_id: str, *, text: str, show_alert: bool = False) -> None:
        await self._finite.run(
            "manual_telegram_answer",
            self._client.answer_callback,
            callback_query_id,
            text=text,
            show_alert=show_alert,
            timeout_seconds=8.0,
        )

    async def reply(
        self,
        *,
        source_message_id: int,
        text: str,
        keyboard: tuple[tuple[str, str], ...],
    ) -> int:
        return await self._finite.run(
            "manual_telegram_reply",
            self._client.send_interaction_reply,
            chat_id=self._chat_id,
            source_message_id=source_message_id,
            text=text,
            keyboard=keyboard,
            timeout_seconds=8.0,
        )

    async def reply_plain(self, *, source_message_id: int, text: str) -> int:
        return await self._finite.run(
            "manual_telegram_reply",
            self._client.send_plain_reply,
            chat_id=self._chat_id,
            source_message_id=source_message_id,
            text=text,
            timeout_seconds=8.0,
        )

    async def edit(
        self,
        *,
        message_id: int,
        text: str,
        keyboard: tuple[tuple[str, str], ...],
    ) -> None:
        await self._finite.run(
            "manual_telegram_edit",
            self._client.edit_interaction,
            chat_id=self._chat_id,
            message_id=message_id,
            text=text,
            keyboard=keyboard,
            timeout_seconds=8.0,
        )


class TelegramTradingControllerPort(Protocol):
    async def handle(self, update: TelegramTradingUpdate) -> str: ...

    async def close(self) -> None: ...


class TelegramTestNewsPort(Protocol):
    async def handle(self, update: TelegramTradingUpdate, *, kind: Literal["futures", "onchain"]) -> str: ...


@dataclass(frozen=True, slots=True)
class TelegramTradingProfileControllers:
    bot: ManualTradingBotAdapter
    manual: ManualTelegramTradingController | None
    onchain: Any | None


def _profile_bot_commands(profile: TelegramTradingProfileControllers) -> tuple[tuple[str, str], ...]:
    commands: list[tuple[str, str]] = [
        ("start", "查看当前账号可用的交易测试指令"),
        ("help", "查看当前账号可用的交易测试指令"),
    ]
    if profile.manual is not None:
        commands.append(("test_futures", "发送 HYPE 合约交易测试新闻"))
        commands.extend(
            (
                ("positions", "查看当前合约持仓"),
                ("history", "查看历史合约持仓"),
                ("trades", "查看合约交易记录"),
            )
        )
    if profile.onchain is not None:
        commands.append(("test_onchain", "发送 BLUECHIP 与 COPPERINU 链上测试新闻"))
    return tuple(commands)


def _profile_help_text(profile: TelegramTradingProfileControllers) -> str:
    visible_commands = [command for command in _profile_bot_commands(profile) if command[0] not in {"start", "help"}]
    command_lines = "\n".join(f"<code>/{name}</code> — {description}" for name, description in visible_commands)
    return (
        "<b>Tracefold 交易测试</b>\n\n"
        "当前 Telegram 账号已绑定独立交易配置。\n\n"
        f"<b>可用指令</b>\n{command_lines}\n\n"
        "测试新闻只会发送到当前私人对话；后续交易只使用当前 Telegram 用户绑定的交易凭证。"
    )


class TelegramTradingUpdateRouter:
    """One Telegram cursor, with lane-specific authorization and independent controllers."""

    def __init__(
        self,
        *,
        profiles: Mapping[int, TelegramTradingProfileControllers],
        test_news: TelegramTestNewsPort,
    ) -> None:
        self._profiles = dict(profiles)
        self._test_news = test_news

    async def close(self) -> None:
        for profile in self._profiles.values():
            if profile.onchain is not None:
                await profile.onchain.close()

    async def handle(self, update: TelegramTradingUpdate) -> str:
        profile = self._profiles.get(update.actor_user_id)
        if update.update_kind == "message":
            if not update.authorized or profile is None:
                return "private_profile_required"
            if update.data == "tf:help:v1":
                await profile.bot.reply(
                    source_message_id=update.message_id,
                    text=_profile_help_text(profile),
                    keyboard=(
                        (("我的交易", "tf:mine:v1"),)
                        if profile.manual is not None
                        else (("链上路由测试", "tf:onchain:v1"),)
                    ),
                )
                return "help_sent"
            if update.data == "tf:test:futures":
                return await self._test_news.handle(update, kind="futures")
            if update.data == "tf:test:onchain":
                return await self._test_news.handle(update, kind="onchain")
            if update.data in {"tf:cmd:positions", "tf:cmd:history", "tf:cmd:trades"}:
                if profile.manual is None:
                    return "manual_unavailable"
                view = {
                    "tf:cmd:positions": "open",
                    "tf:cmd:history": "closed",
                    "tf:cmd:trades": "events",
                }[update.data]
                return await profile.manual.handle_command(update, view=view)
            return "message_ignored"
        if profile is None:
            return "profile_unavailable"
        if not update.authorized:
            await profile.bot.answer(
                update.callback_query_id,
                text="只能在你与机器人的私人对话中操作。",
                show_alert=True,
            )
            return "private_profile_required"
        if update.data == "tf:onchain:v1" or update.data.startswith("tf:o:"):
            if profile.onchain is None:
                await profile.bot.answer(update.callback_query_id, text="你的链上交易账户当前未配置。", show_alert=True)
                return "onchain_unavailable"
            return str(await profile.onchain.handle(update))
        if profile.manual is None:
            await profile.bot.answer(update.callback_query_id, text="你的合约交易账户当前未配置。", show_alert=True)
            return "manual_unavailable"
        return await profile.manual.handle(update)


class ManualTradingRunner:
    """Poll one Telegram callback stream; settlement advances the cursor atomically."""

    def __init__(
        self,
        *,
        database: WorkerTradingDatabase,
        client: TelegramTradingClient,
        controller: TelegramTradingControllerPort,
        finite: FiniteOperations,
        poll_seconds: float,
        command_menus: Mapping[int, tuple[tuple[str, str], ...]] | None = None,
        clock_ms: Any | None = None,
    ) -> None:
        self._database = database
        self._client = client
        self._controller = controller
        self._finite = finite
        self._poll_seconds = float(poll_seconds)
        self._pending_command_menus = dict(command_menus or {})
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)

    async def close(self) -> None:
        await self._controller.close()
        self._client.close()

    async def run(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                await self.turn()
            except Exception as exc:
                logger.error("manual Telegram trading turn failed error={}", type(exc).__name__)
            with suppress(TimeoutError):
                await asyncio.wait_for(stop_event.wait(), timeout=self._poll_seconds)

    async def turn(self) -> int:
        await self._sync_command_menus()
        cursor = await self._database.read(
            "manual_telegram_cursor",
            lambda repos: repos.trading.manual_next_telegram_update_id(),
            timeout_seconds=3.0,
        )
        updates: tuple[TelegramTradingUpdate, ...] = await self._finite.run(
            "manual_telegram_poll",
            self._client.poll_updates,
            next_update_id=cursor,
            timeout_seconds=8.0,
        )
        processed = 0
        for update in updates:
            claimed = await self._database.tx(
                "manual_telegram_claim",
                partial(_claim_update, update=update, now_ms=int(self._clock_ms())),
                timeout_seconds=3.0,
            )
            state: str | None = "RECEIVED"
            if not claimed:
                state = await self._database.read(
                    "manual_telegram_claim_state",
                    partial(_telegram_update_state, update_id=update.update_id),
                    timeout_seconds=3.0,
                )
            if state == "SETTLED":
                continue
            if state != "RECEIVED":
                raise RuntimeError("manual_telegram_update_claim_invalid")
            result = await self._controller.handle(update)
            settled = await self._database.tx(
                "manual_telegram_settle",
                partial(
                    _settle_update,
                    update_id=update.update_id,
                    result_code=result,
                    now_ms=int(self._clock_ms()),
                ),
                timeout_seconds=3.0,
            )
            if not settled:
                raise RuntimeError("manual_telegram_update_settlement_conflict")
            processed += 1
        await self._deliver_notifications(limit=3)
        return processed

    async def _sync_command_menus(self) -> None:
        for chat_id, commands in tuple(self._pending_command_menus.items()):
            try:
                await self._finite.run(
                    "manual_telegram_command_menu",
                    self._client.set_commands,
                    chat_id=chat_id,
                    commands=commands,
                    timeout_seconds=8.0,
                )
            except Exception as exc:
                logger.warning(
                    "manual Telegram command menu sync failed chat_id={} error={}",
                    chat_id,
                    type(exc).__name__,
                )
            else:
                self._pending_command_menus.pop(chat_id, None)

    async def _deliver_notifications(self, *, limit: int) -> None:
        await self._database.tx(
            "manual_notification_recovery",
            partial(_recover_notifications, now_ms=int(self._clock_ms())),
            timeout_seconds=3.0,
        )
        for _ in range(limit):
            notification = await self._database.tx(
                "manual_notification_begin",
                partial(_begin_notification, now_ms=int(self._clock_ms())),
                timeout_seconds=3.0,
            )
            if notification is None:
                return
            rendered = _render_manual_notification(notification)
            await self._deliver_interaction_effect(notification, rendered=rendered)
            await self._deliver_reply_effect(notification, rendered=rendered)

    async def _deliver_interaction_effect(self, notification: Mapping[str, Any], *, rendered: str) -> None:
        notification_id = str(notification["notification_id"])
        interaction_state = str(notification.get("interaction_state") or "")
        if interaction_state in {"SENT", "AMBIGUOUS", "SKIPPED"}:
            return
        if interaction_state != "PENDING":
            raise RuntimeError("manual_notification_interaction_state_invalid")
        interaction_message_id = notification.get("interaction_message_id")
        if not isinstance(interaction_message_id, int) or isinstance(interaction_message_id, bool):
            skipped = await self._database.tx(
                "manual_notification_interaction_skip",
                partial(_skip_notification_interaction, notification_id=notification_id),
                timeout_seconds=3.0,
            )
            if not skipped:
                raise RuntimeError("manual_notification_interaction_skip_conflict")
            return
        begun = await self._database.tx(
            "manual_notification_interaction_begin",
            partial(
                _begin_notification_effect,
                notification_id=notification_id,
                effect="interaction",
                now_ms=int(self._clock_ms()),
            ),
            timeout_seconds=3.0,
        )
        if not begun:
            raise RuntimeError("manual_notification_interaction_begin_conflict")
        try:
            await self._finite.run(
                "manual_notification_interaction_edit",
                self._client.edit_interaction,
                chat_id=int(notification.get("chat_id") or 0),
                message_id=interaction_message_id,
                text=rendered,
                keyboard=(("我的交易", "tf:mine:v1"),),
                timeout_seconds=8.0,
            )
        except Exception as exc:
            marked = await self._database.tx(
                "manual_notification_interaction_ambiguous",
                partial(
                    _mark_notification_interaction_ambiguous,
                    notification_id=notification_id,
                    error_code=f"interaction_{type(exc).__name__}",
                    now_ms=int(self._clock_ms()),
                ),
                timeout_seconds=3.0,
            )
            if not marked:
                raise RuntimeError("manual_notification_interaction_settlement_conflict") from exc
            return
        settled = await self._database.tx(
            "manual_notification_interaction_settle",
            partial(
                _settle_notification_interaction,
                notification_id=notification_id,
                now_ms=int(self._clock_ms()),
            ),
            timeout_seconds=3.0,
        )
        if not settled:
            raise RuntimeError("manual_notification_interaction_settlement_conflict")

    async def _deliver_reply_effect(self, notification: Mapping[str, Any], *, rendered: str) -> None:
        notification_id = str(notification["notification_id"])
        if notification.get("reply_state") != "PENDING":
            raise RuntimeError("manual_notification_reply_state_invalid")
        begun = await self._database.tx(
            "manual_notification_reply_begin",
            partial(
                _begin_notification_effect,
                notification_id=notification_id,
                effect="reply",
                now_ms=int(self._clock_ms()),
            ),
            timeout_seconds=3.0,
        )
        if not begun:
            raise RuntimeError("manual_notification_reply_begin_conflict")
        try:
            message_id: int = await self._finite.run(
                "manual_notification_reply",
                self._client.send_interaction_reply,
                chat_id=int(notification.get("chat_id") or 0),
                source_message_id=int(notification["source_message_id"]),
                text=rendered,
                keyboard=(("我的交易", "tf:mine:v1"),),
                timeout_seconds=8.0,
            )
        except Exception as exc:
            marked = await self._database.tx(
                "manual_notification_reply_ambiguous",
                partial(
                    _mark_notification_ambiguous,
                    notification_id=notification_id,
                    error_code=f"reply_{type(exc).__name__}",
                    now_ms=int(self._clock_ms()),
                ),
                timeout_seconds=3.0,
            )
            if not marked:
                raise RuntimeError("manual_notification_reply_settlement_conflict") from exc
            return
        settled = await self._database.tx(
            "manual_notification_settle",
            partial(
                _settle_notification,
                notification_id=notification_id,
                provider_message_id=message_id,
                now_ms=int(self._clock_ms()),
            ),
            timeout_seconds=3.0,
        )
        if not settled:
            raise RuntimeError("manual_notification_settlement_conflict")


def _claim_update(repos: Any, *, update: TelegramTradingUpdate, now_ms: int) -> bool:
    return bool(repos.trading.claim_manual_telegram_update(update, now_ms=now_ms))


def _telegram_update_state(repos: Any, *, update_id: int) -> str | None:
    value = repos.trading.manual_telegram_update_state(update_id)
    return str(value) if value is not None else None


def _settle_update(repos: Any, *, update_id: int, result_code: str, now_ms: int) -> bool:
    return bool(
        repos.trading.settle_manual_telegram_update(
            update_id,
            result_code=result_code,
            now_ms=now_ms,
        )
    )


def _begin_notification(repos: Any, *, now_ms: int) -> dict[str, Any] | None:
    value = repos.trading.begin_manual_notification(now_ms=now_ms)
    return None if value is None else dict(value)


def _recover_notifications(repos: Any, *, now_ms: int) -> int:
    return int(repos.trading.terminalize_stale_manual_notifications(now_ms=now_ms))


def _begin_notification_effect(
    repos: Any,
    *,
    notification_id: str,
    effect: Literal["interaction", "reply"],
    now_ms: int,
) -> bool:
    return bool(
        repos.trading.begin_manual_notification_effect(
            notification_id,
            effect=effect,
            now_ms=now_ms,
        )
    )


def _skip_notification_interaction(repos: Any, *, notification_id: str) -> bool:
    return bool(repos.trading.skip_manual_notification_interaction(notification_id))


def _settle_notification_interaction(repos: Any, *, notification_id: str, now_ms: int) -> bool:
    return bool(repos.trading.settle_manual_notification_interaction(notification_id, now_ms=now_ms))


def _mark_notification_interaction_ambiguous(
    repos: Any,
    *,
    notification_id: str,
    error_code: str,
    now_ms: int,
) -> bool:
    return bool(
        repos.trading.mark_manual_notification_interaction_ambiguous(
            notification_id,
            error_code=error_code,
            now_ms=now_ms,
        )
    )


def _settle_notification(
    repos: Any,
    *,
    notification_id: str,
    provider_message_id: int,
    now_ms: int,
) -> bool:
    return bool(
        repos.trading.settle_manual_notification(
            notification_id,
            provider_message_id=provider_message_id,
            now_ms=now_ms,
        )
    )


def _mark_notification_ambiguous(
    repos: Any,
    *,
    notification_id: str,
    error_code: str,
    now_ms: int,
) -> bool:
    return bool(
        repos.trading.mark_manual_notification_ambiguous(
            notification_id,
            error_code=error_code,
            now_ms=now_ms,
        )
    )


def _render_manual_notification(notification: Mapping[str, Any]) -> str:
    kind = str(notification.get("notification_kind") or "")
    payload = notification.get("payload")
    values = payload if isinstance(payload, Mapping) else {}
    if kind == "PROTECTION_REJECTED":
        failed_leg = "止损" if values.get("leg") == "stop_loss" else "止盈"
        return (
            "<b>🚨 保护单被拒，仓位仍有敞口</b>\n\n"
            f"失败环节：<b>{failed_leg}</b>\n"
            "请立即前往 Binance 正式账户检查仓位并人工处理。"
            "系统已停止自动执行，不会重发该保护单。"
        )
    if kind == "POSITION_OPENED":
        receipt = values.get("receipt")
        fill = receipt if isinstance(receipt, Mapping) else {}
        quantity = html.escape(str(fill.get("executed_quantity") or "待仓位对账"))
        price = html.escape(str(fill.get("average_price") or "待仓位对账"))
        symbol = html.escape(str(values.get("symbol") or "标的"))
        side = "做多" if values.get("side") == "long" else "做空"
        return (
            "<b>✅ Binance 正式账户仓位已成交</b>\n\n"
            f"标的 / 方向：{symbol} {side}\n成交数量：{quantity}\n成交均价：{price}\n\n"
            "可发送 /positions 查看实时仓位、止盈止损和盈亏。"
        )
    if kind == "POSITION_CLOSED":
        exit_price = html.escape(str(values.get("exit_price") or "待核对"))
        pnl = html.escape(str(values.get("realized_pnl_usd") or "待核对"))
        entry_price = html.escape(str(values.get("entry_price") or "待核对"))
        symbol = html.escape(str(values.get("symbol") or "标的"))
        side = "做多" if values.get("side") == "long" else "做空"
        holding_ms = values.get("holding_time_ms")
        holding = _format_holding_time(holding_ms)
        reason = "手动平仓" if values.get("exit_reason") == "manual_close" else "止盈、止损或交易所侧平仓"
        return (
            "<b>🏁 仓位已关闭</b>\n\n"
            f"标的 / 方向：{symbol} {side}\n入场价格：{entry_price}\n退出价格：{exit_price}\n"
            f"已实现盈亏：{pnl} USDT\n持仓时长：{holding}\n退出方式：{reason}\n\n"
            "可发送 /history 查看完整历史持仓。"
        )
    label = {
        "POSITION_OPENED": "✅ Binance 正式账户仓位已成交",
        "TP_CREATED": "🟢 止盈保护单已创建",
        "SL_CREATED": "🔴 止损保护单已创建",
        "POSITION_CLOSED": "🏁 仓位已关闭",
        "ORDER_REJECTED": "❌ 订单被交易所拒绝",
        "ORDER_AMBIGUOUS": "⚠️ 订单结果待对账",
    }.get(kind, "交易状态已更新")
    client_id = str(values.get("client_id") or "")
    suffix = f"\n客户端订单 ID：<code>{html.escape(client_id)}</code>" if client_id else ""
    return f"<b>{label}</b>\n\n该状态来自持久化交易账本。{suffix}"


def _format_holding_time(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return "待核对"
    total_seconds = value // 1_000
    hours, remainder = divmod(total_seconds, 3_600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}小时{minutes}分{seconds}秒"
    if minutes:
        return f"{minutes}分{seconds}秒"
    return f"{seconds}秒"


def _fresh_timestamp(value: object, now_ms: int, max_age_ms: int) -> bool:
    if isinstance(value, bool) or not isinstance(value, int):
        return False
    return now_ms - max_age_ms <= value <= now_ms + 5_000


def _wire_manual_trading(
    *,
    settings: Settings,
    db: WorkerDatabase,
    finite: FiniteOperations,
) -> ManualTradingRunner | None:
    """Wire one Telegram ingress to independent futures and onchain controllers."""

    manual_availability = manual_trading_availability(settings, inspect_secret_files=False)
    onchain_availability = onchain_trading_availability(settings, inspect_secret_files=False)
    if not manual_availability.requested and not onchain_availability.requested:
        return None
    if manual_availability.requested and not manual_availability.interaction_available:
        raise RuntimeError(f"manual_trading_unavailable:{manual_availability.reason or 'configuration_invalid'}")
    if onchain_availability.requested and not onchain_availability.interaction_available:
        raise RuntimeError(f"onchain_trading_unavailable:{onchain_availability.reason or 'configuration_invalid'}")
    token_file = settings.news_telegram_bot_token_file()
    profiles = settings.trading.telegram_profiles
    if token_file is None or not profiles:
        raise RuntimeError("manual_trading_unavailable:telegram_configuration_invalid")
    try:
        bot_token = read_secure_secret_text(token_file)
    except SecretFileError:
        raise RuntimeError("manual_trading_unavailable:telegram_bot_token_unavailable") from None
    authorized_users = tuple(profile.user_id for profile in profiles)
    try:
        client = TelegramTradingClient(
            bot_token=bot_token,
            authorized_user_ids=authorized_users,
        )
    except ValueError:
        raise RuntimeError("manual_trading_unavailable:telegram_client_invalid") from None
    database = WorkerTradingDatabase(db)
    from tracefold.app.workers.wiring.onchain_trading import wire_onchain_controller

    profile_controllers: dict[int, TelegramTradingProfileControllers] = {}
    policy = settings.trading.manual
    for profile in profiles:
        bot = ManualTradingBotAdapter(client, finite, chat_id=profile.user_id)
        target_sha256 = client.target_sha256_for(profile.user_id)
        manual_profile = manual_trading_profile_availability(settings, profile, inspect_secret_files=False)
        manual_controller: ManualTelegramTradingController | None = None
        if manual_profile.requested:
            account = profile.manual
            repository = ManualTradingRepositoryAdapter(database, target_sha256=target_sha256)
            snapshot_reader = ManualAccountMarketSnapshotReader(database, account_ref=account.account_ref)
            manual_controller = ManualTelegramTradingController(
                repository=repository,
                bot=bot,
                snapshot_reader=snapshot_reader,
                config=ManualTradingControllerConfig(
                    account_ref=account.account_ref,
                    venue=account.venue,
                    risk=ManualRiskConfig(**policy.risk.model_dump()),
                    presets={
                        StrategyPreset.TIGHT_STOP: ManualStrategyPresetConfig(
                            preset=StrategyPreset.TIGHT_STOP,
                            **policy.tight_stop.model_dump(),
                        ),
                        StrategyPreset.WIDE_STOP: ManualStrategyPresetConfig(
                            preset=StrategyPreset.WIDE_STOP,
                            **policy.wide_stop.model_dump(),
                        ),
                    },
                ),
            )
        onchain_controller = wire_onchain_controller(
            settings=settings,
            profile=profile,
            database=database,
            bot=bot,
            target_sha256=target_sha256,
        )
        profile_controllers[profile.user_id] = TelegramTradingProfileControllers(
            bot=bot,
            manual=manual_controller,
            onchain=onchain_controller,
        )
    test_news = TelegramDevelopmentTestNewsController(
        settings=settings,
        database=database,
        client=client,
        finite=finite,
    )
    controller = TelegramTradingUpdateRouter(
        profiles=profile_controllers,
        test_news=test_news,
    )
    return ManualTradingRunner(
        database=database,
        client=client,
        controller=controller,
        finite=finite,
        poll_seconds=_MANUAL_TELEGRAM_POLL_SECONDS,
        command_menus={user_id: _profile_bot_commands(profile) for user_id, profile in profile_controllers.items()},
    )


__all__ = [
    "ManualAccountMarketSnapshotReader",
    "ManualTradingBotAdapter",
    "ManualTradingRepositoryAdapter",
    "ManualTradingRunner",
    "TelegramTradingUpdateRouter",
    "_wire_manual_trading",
    "manual_trade_sources_from_development_test_news",
    "manual_trade_sources_from_news_projection",
]
