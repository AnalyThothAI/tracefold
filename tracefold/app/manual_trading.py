"""App-owned Telegram -> News projection -> manual Trading composition seam."""

from __future__ import annotations

import html
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Protocol
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from tracefold.integrations.telegram import TelegramTradingUpdate
from tracefold.trading import (
    MAX_DEVELOPMENT_TEST_NOTIONAL_USD,
    ManualAccountSnapshot,
    ManualModificationGuard,
    ManualPositionState,
    ManualPositionView,
    ManualRiskConfig,
    ManualSessionState,
    ManualStrategyPresetConfig,
    ManualTargetPicker,
    ManualTargetPickerState,
    ManualTradeHistoryEvent,
    ManualTradeIntent,
    ManualTradeParameters,
    ManualTradePreview,
    ManualTradeSession,
    ManualTradeSource,
    ManualVenue,
    ModificationGuardState,
    StrategyPreset,
    TradeSide,
    build_manual_trade_preview,
    create_manual_trade_intent,
    guard_manual_trade_modification,
    is_development_test_source,
    recommend_manual_trade,
)


class ManualTradingRepositoryPort(Protocol):
    async def sources_for_message(self, message_id: int) -> tuple[ManualTradeSource, ...]: ...

    async def begin_target_picker(
        self,
        *,
        picker_id: str,
        sources: tuple[ManualTradeSource, ...],
        actor_user_id: int,
        chat_id: int,
        now_ms: int,
    ) -> tuple[ManualTargetPicker, bool]: ...

    async def begin_target_picker_reply(self, picker_id: str, *, now_ms: int) -> bool: ...

    async def attach_target_picker_message(self, picker_id: str, *, message_id: int, now_ms: int) -> bool: ...

    async def get_target_picker(self, picker_id: str) -> ManualTargetPicker | None: ...

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
    ) -> tuple[ManualTradeSession, bool]: ...

    async def begin_interaction_reply(self, session_id: str, *, now_ms: int) -> bool: ...

    async def attach_interaction_message(self, session_id: str, *, message_id: int, now_ms: int) -> bool: ...

    async def get_session(self, session_id: str) -> ManualTradeSession | None: ...

    async def list_sessions(self, *, actor_user_id: int, chat_id: int) -> tuple[ManualTradeSession, ...]: ...

    async def list_positions(
        self,
        *,
        actor_user_id: int,
        chat_id: int,
        state: str,
    ) -> tuple[ManualPositionView, ...]: ...

    async def get_position(
        self,
        *,
        session_id: str,
        actor_user_id: int,
        chat_id: int,
    ) -> ManualPositionView | None: ...

    async def list_trade_events(
        self,
        *,
        actor_user_id: int,
        chat_id: int,
    ) -> tuple[ManualTradeHistoryEvent, ...]: ...

    async def request_close(
        self,
        *,
        session_id: str,
        actor_user_id: int,
        chat_id: int,
        requested_bps: int,
        update_id: int,
        now_ms: int,
    ) -> bool: ...

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
    ) -> ManualTradeSession | None: ...

    async def confirm_intent(
        self,
        intent: ManualTradeIntent,
        *,
        update_id: int,
        result_code: str,
        now_ms: int,
    ) -> bool: ...

    async def cancel_session(
        self,
        session_id: str,
        *,
        update_id: int,
        result_code: str,
        now_ms: int,
    ) -> bool: ...


class ManualTradingBotPort(Protocol):
    async def answer(self, callback_query_id: str, *, text: str, show_alert: bool = False) -> None: ...

    async def reply(
        self,
        *,
        source_message_id: int,
        text: str,
        keyboard: tuple[tuple[str, str], ...],
    ) -> int: ...

    async def edit(
        self,
        *,
        message_id: int,
        text: str,
        keyboard: tuple[tuple[str, str], ...],
    ) -> None: ...


class ManualAccountSnapshotReader(Protocol):
    async def snapshot_for(self, source: ManualTradeSource) -> ManualAccountSnapshot: ...


@dataclass(frozen=True, slots=True)
class ManualTradingControllerConfig:
    account_ref: str
    venue: ManualVenue
    risk: ManualRiskConfig
    presets: Mapping[StrategyPreset, ManualStrategyPresetConfig]

    def __post_init__(self) -> None:
        if set(self.presets) != {StrategyPreset.TIGHT_STOP, StrategyPreset.WIDE_STOP}:
            raise ValueError("manual_trading_presets_incomplete")
        if any(key is not value.preset for key, value in self.presets.items()):
            raise ValueError("manual_trading_preset_identity_mismatch")


class ManualTelegramTradingController:
    """Handle one already claimed callback without holding a database transaction over I/O."""

    def __init__(
        self,
        *,
        repository: ManualTradingRepositoryPort,
        bot: ManualTradingBotPort,
        snapshot_reader: ManualAccountSnapshotReader,
        config: ManualTradingControllerConfig,
        clock_ms: Callable[[], int] | None = None,
        session_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._repository = repository
        self._bot = bot
        self._snapshot_reader = snapshot_reader
        self._config = config
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._session_id_factory = session_id_factory or (lambda: str(uuid4()))

    async def handle(self, update: TelegramTradingUpdate) -> str:
        if not update.authorized:
            await self._bot.answer(update.callback_query_id, text="你没有交易权限。", show_alert=True)
            return "unauthorized"
        if update.data == "tf:trade:v1":
            return await self._start_trade(update)
        if update.data == "tf:detail:v1":
            return await self._show_detail(update)
        portfolio_action = _parse_portfolio_action(update.data)
        if portfolio_action is not None:
            return await self._handle_portfolio_action(update, *portfolio_action)
        target_action = _parse_target_action(update.data)
        if target_action is not None:
            return await self._select_target(update, *target_action)
        parsed = _parse_session_action(update.data)
        if parsed is None:
            await self._bot.answer(update.callback_query_id, text="操作无效或已过期。", show_alert=True)
            return "action_invalid"
        action, argument, session_id = parsed
        session = await self._repository.get_session(session_id)
        if not _session_bound_to_update(session, update):
            await self._bot.answer(update.callback_query_id, text="该操作不属于当前会话。", show_alert=True)
            return "session_binding_mismatch"
        if session is None:
            raise RuntimeError("manual_trading_session_binding_invariant")
        if session.last_effect_update_id == update.update_id and session.last_effect_result_code is not None:
            await self._render_replayed_effect(update, session)
            return session.last_effect_result_code
        if action == "preset":
            if argument is None:
                raise RuntimeError("manual_trading_preset_argument_invariant")
            return await self._select_preset(update, session, argument)
        if action == "modify":
            await self._bot.edit(
                message_id=update.message_id,
                text=_render_modification_menu(session),
                keyboard=_modification_keyboard(session.session_id),
            )
            await self._bot.answer(update.callback_query_id, text="请选择要调整的参数。")
            return "modification_opened"
        if action == "adjust":
            if argument is None:
                raise RuntimeError("manual_trading_adjust_argument_invariant")
            return await self._adjust(update, session, argument)
        if action == "back":
            await self._render_session(update, session)
            return "preview_restored"
        if action in {"confirm", "high_risk_confirm"}:
            return await self._confirm(update, session, high_risk=action == "high_risk_confirm")
        if action == "cancel":
            return await self._cancel(update, session)
        await self._bot.answer(update.callback_query_id, text="操作暂不可用。", show_alert=True)
        return "action_unavailable"

    async def handle_command(self, update: TelegramTradingUpdate, *, view: str) -> str:
        if not update.authorized or update.update_kind != "message":
            return "private_profile_required"
        return await self._show_portfolio(update, view=view, edit=False)

    async def _handle_portfolio_action(
        self,
        update: TelegramTradingUpdate,
        action: str,
        argument: str | None,
        session_id: str | None,
    ) -> str:
        if action == "menu":
            return await self._show_portfolio(update, view="menu", edit=True)
        if action in {"open", "closed", "events"}:
            return await self._show_portfolio(update, view=action, edit=True)
        if session_id is None:
            await self._bot.answer(update.callback_query_id, text="持仓操作无效。", show_alert=True)
            return "position_action_invalid"
        position = await self._repository.get_position(
            session_id=session_id,
            actor_user_id=update.actor_user_id,
            chat_id=update.chat_id,
        )
        if position is None:
            await self._bot.answer(update.callback_query_id, text="持仓不存在或不属于当前账号。", show_alert=True)
            return "position_unavailable"
        if action == "position":
            await self._bot.edit(
                message_id=update.message_id,
                text=_render_position_detail(position),
                keyboard=_position_keyboard(position),
            )
            await self._bot.answer(update.callback_query_id, text="持仓已刷新。")
            return "position_shown"
        if action == "news":
            await self._bot.reply(
                source_message_id=position.source.delivery_message_id,
                text=(
                    f"📰 <b>{html.escape(position.source.base_symbol)} 关联新闻</b>\n\n"
                    "这条回复已绑定到产生该交易的原始新闻；点击上方回复引用即可回到新闻。"
                ),
                keyboard=(("返回持仓", f"tf:pos:{position.session_id}"),),
            )
            await self._bot.answer(update.callback_query_id, text="已定位关联新闻。")
            return "position_news_linked"
        if action == "close":
            if position.state not in {ManualPositionState.OPEN, ManualPositionState.EXPOSED} or position.quantity <= 0:
                await self._bot.answer(update.callback_query_id, text="当前持仓不可发起平仓。", show_alert=True)
                return "position_close_unavailable"
            await self._bot.edit(
                message_id=update.message_id,
                text=_render_close_selector(position),
                keyboard=_close_selector_keyboard(position.session_id),
            )
            await self._bot.answer(update.callback_query_id, text="请选择平仓比例。")
            return "position_close_opened"
        if action == "close_preview":
            requested_bps = _close_bps(argument)
            await self._bot.edit(
                message_id=update.message_id,
                text=_render_close_confirmation(position, requested_bps=requested_bps),
                keyboard=_close_confirmation_keyboard(position.session_id, requested_bps=requested_bps),
            )
            await self._bot.answer(update.callback_query_id, text="请再次确认平仓。")
            return "position_close_preview"
        if action == "close_confirm":
            requested_bps = _close_bps(argument)
            requested = await self._repository.request_close(
                session_id=position.session_id,
                actor_user_id=update.actor_user_id,
                chat_id=update.chat_id,
                requested_bps=requested_bps,
                update_id=update.update_id,
                now_ms=int(self._clock_ms()),
            )
            if not requested:
                await self._bot.answer(
                    update.callback_query_id,
                    text="平仓请求未创建；持仓状态可能已变化或已有请求待处理。",
                    show_alert=True,
                )
                return "position_close_conflict"
            await self._bot.edit(
                message_id=update.message_id,
                text=(
                    f"⏳ <b>{html.escape(position.source.base_symbol)} 平仓请求已确认</b>\n\n"
                    f"比例：{_pct(requested_bps)}\n"
                    "执行器将使用 reduce-only 市价单，并以交易所持仓为准持续对账。"
                ),
                keyboard=(("查看当前持仓", "tf:mine:open"), ("交易记录", "tf:mine:events")),
            )
            await self._bot.answer(update.callback_query_id, text="平仓请求已提交。")
            return "position_close_requested"
        await self._bot.answer(update.callback_query_id, text="持仓操作暂不可用。", show_alert=True)
        return "position_action_unavailable"

    async def _show_portfolio(self, update: TelegramTradingUpdate, *, view: str, edit: bool) -> str:
        if view == "menu":
            text = "📋 <b>我的交易</b>\n\n请选择要查看的内容。"
            keyboard = _portfolio_menu_keyboard()
        elif view in {"open", "closed"}:
            positions = await self._repository.list_positions(
                actor_user_id=update.actor_user_id,
                chat_id=update.chat_id,
                state=view,
            )
            text = _render_position_list(positions, closed=view == "closed")
            keyboard = _position_list_keyboard(positions, closed=view == "closed")
        elif view == "events":
            events = await self._repository.list_trade_events(
                actor_user_id=update.actor_user_id,
                chat_id=update.chat_id,
            )
            text = _render_trade_events(events)
            keyboard = (("当前持仓", "tf:mine:open"), ("历史持仓", "tf:mine:closed"), ("返回", "tf:mine:v1"))
        else:
            raise ValueError("manual_portfolio_view_invalid")
        if edit:
            await self._bot.edit(message_id=update.message_id, text=text, keyboard=keyboard)
            await self._bot.answer(update.callback_query_id, text="交易数据已刷新。")
        else:
            await self._bot.reply(source_message_id=update.message_id, text=text, keyboard=keyboard)
        return f"portfolio_{view}_shown"

    async def _start_trade(self, update: TelegramTradingUpdate) -> str:
        sources = await self._repository.sources_for_message(update.message_id)
        if not sources:
            await self._bot.answer(update.callback_query_id, text="这条消息没有可交易标的。", show_alert=True)
            return "source_unavailable"
        if len(sources) > 1:
            try:
                picker, created = await self._repository.begin_target_picker(
                    picker_id=_canonical_session_id(self._session_id_factory()),
                    sources=sources,
                    actor_user_id=update.actor_user_id,
                    chat_id=update.chat_id,
                    now_ms=int(self._clock_ms()),
                )
            except ValueError as exc:
                if str(exc) == "manual_target_picker_session_active":
                    await self._bot.answer(
                        update.callback_query_id,
                        text="这条新闻已有进行中的交易会话，请使用先前的交互消息。",
                        show_alert=True,
                    )
                    return "target_session_active"
                await self._bot.answer(
                    update.callback_query_id,
                    text="TG 新闻展示标的已变化或状态不明确，请重新打开交易。",
                    show_alert=True,
                )
                return "target_picker_stale"
            if picker.interaction_message_id is not None:
                await self._bot.answer(update.callback_query_id, text="请在已发送的标的选择消息中继续。")
                return "target_picker_resumed"
            if not await self._repository.begin_target_picker_reply(
                picker.picker_id,
                now_ms=int(self._clock_ms()),
            ):
                await self._bot.answer(
                    update.callback_query_id,
                    text="标的选择消息发送结果待确认；为避免重复消息，本次不会重发。",
                    show_alert=True,
                )
                return "target_picker_ambiguous"
            interaction_message_id = await self._bot.reply(
                source_message_id=update.message_id,
                text=_render_target_selector(),
                keyboard=_target_keyboard(sources, picker_id=picker.picker_id),
            )
            if not await self._repository.attach_target_picker_message(
                picker.picker_id,
                message_id=interaction_message_id,
                now_ms=int(self._clock_ms()),
            ):
                await self._bot.answer(update.callback_query_id, text="标的选择消息绑定失败。", show_alert=True)
                return "target_picker_attach_failed"
            await self._bot.answer(update.callback_query_id, text="请先选择交易标的。")
            return "target_picker_created" if created else "target_picker_resumed"
        return await self._begin_selected_source(
            update,
            sources[0],
            picker_id=None,
            reuse_interaction_message=False,
        )

    async def _select_target(
        self,
        update: TelegramTradingUpdate,
        symbol: str,
        picker_id: str,
    ) -> str:
        picker = await self._repository.get_target_picker(picker_id)
        if not _picker_bound_to_update(picker, update):
            await self._bot.answer(update.callback_query_id, text="该标的选择不属于当前消息。", show_alert=True)
            return "target_picker_binding_mismatch"
        if picker is None:
            raise RuntimeError("manual_target_picker_binding_invariant")
        if picker.state is ManualTargetPickerState.CONSUMED:
            if picker.selected_symbol != symbol:
                await self._bot.answer(
                    update.callback_query_id,
                    text="这次标的选择已经确定，不能更换。",
                    show_alert=True,
                )
                return "source_conflict"
            source = next((candidate for candidate in picker.sources if candidate.base_symbol == symbol), None)
            if source is None:
                raise RuntimeError("manual_target_picker_consumed_source_invariant")
            return await self._begin_selected_source(
                update,
                source,
                picker_id=picker_id,
                reuse_interaction_message=True,
            )
        sources = await self._repository.sources_for_message(picker.source_message_id)
        if sources != picker.sources:
            await self._bot.answer(
                update.callback_query_id,
                text="TG 新闻展示标的已变化或状态不明确，请重新打开交易。",
                show_alert=True,
            )
            return "target_picker_stale"
        source = next((candidate for candidate in sources if candidate.base_symbol == symbol), None)
        if source is None:
            await self._bot.answer(
                update.callback_query_id,
                text="该标的不在这条 TG 新闻的展示标的中。",
                show_alert=True,
            )
            return "target_invalid"
        return await self._begin_selected_source(
            update,
            source,
            picker_id=picker_id,
            reuse_interaction_message=True,
        )

    async def _begin_selected_source(
        self,
        update: TelegramTradingUpdate,
        source: ManualTradeSource,
        *,
        picker_id: str | None,
        reuse_interaction_message: bool,
    ) -> str:
        try:
            session, created = await self._repository.begin_session(
                session_id=_canonical_session_id(self._session_id_factory()),
                source=source,
                picker_id=picker_id,
                actor_user_id=update.actor_user_id,
                chat_id=update.chat_id,
                update_id=update.update_id,
                now_ms=int(self._clock_ms()),
            )
        except ValueError:
            await self._bot.answer(
                update.callback_query_id,
                text="这条新闻已有交易会话，不能更换已选标的。",
                show_alert=True,
            )
            return "source_conflict"
        if session.interaction_message_id is None:
            if reuse_interaction_message:
                attached = await self._repository.attach_interaction_message(
                    session.session_id,
                    message_id=update.message_id,
                    now_ms=int(self._clock_ms()),
                )
                if not attached:
                    await self._bot.answer(update.callback_query_id, text="交易会话创建失败。", show_alert=True)
                    return "session_attach_failed"
                await self._bot.edit(
                    message_id=update.message_id,
                    text=_render_strategy_selector(source),
                    keyboard=_strategy_keyboard(session.session_id),
                )
            else:
                if not await self._repository.begin_interaction_reply(
                    session.session_id,
                    now_ms=int(self._clock_ms()),
                ):
                    await self._bot.answer(
                        update.callback_query_id,
                        text="交互消息发送结果待确认；为避免重复消息，本次不会重发。",
                        show_alert=True,
                    )
                    return "interaction_reply_ambiguous"
                message_id = await self._bot.reply(
                    source_message_id=update.message_id,
                    text=_render_strategy_selector(source),
                    keyboard=_strategy_keyboard(session.session_id),
                )
                attached = await self._repository.attach_interaction_message(
                    session.session_id,
                    message_id=message_id,
                    now_ms=int(self._clock_ms()),
                )
                if not attached:
                    await self._bot.answer(update.callback_query_id, text="交易会话创建失败。", show_alert=True)
                    return "session_attach_failed"
        elif reuse_interaction_message:
            if session.interaction_message_id != update.message_id:
                await self._bot.answer(
                    update.callback_query_id,
                    text="这条新闻已有交易会话，请使用先前的交互消息。",
                    show_alert=True,
                )
                return "session_already_open"
            if session.state is ManualSessionState.AWAITING_STRATEGY:
                await self._bot.edit(
                    message_id=update.message_id,
                    text=_render_strategy_selector(session.source),
                    keyboard=_strategy_keyboard(session.session_id),
                )
            else:
                await self._render_session(update, session)
                return "session_resumed"
        await self._bot.answer(update.callback_query_id, text="请选择交易策略。")
        return "session_created" if created else "session_resumed"

    async def _show_detail(self, update: TelegramTradingUpdate) -> str:
        sources = await self._repository.sources_for_message(update.message_id)
        if not sources:
            await self._bot.answer(update.callback_query_id, text="详细数据暂不可用。", show_alert=True)
            return "detail_unavailable"
        symbols = "/".join(source.base_symbol for source in sources)
        side = sources[0].side
        await self._bot.answer(
            update.callback_query_id,
            text=(
                f"{symbols} {_side_zh(side)}：行情、关联复核和观点已在原新闻中原位补全；"
                "交易预览会另取最新账户与市场快照。"
            ),
            show_alert=True,
        )
        return "detail_shown"

    async def _select_preset(
        self,
        update: TelegramTradingUpdate,
        session: ManualTradeSession,
        argument: str,
    ) -> str:
        preset = {"t": StrategyPreset.TIGHT_STOP, "w": StrategyPreset.WIDE_STOP}.get(argument)
        if preset is None or session.state is not ManualSessionState.AWAITING_STRATEGY:
            await self._bot.answer(update.callback_query_id, text="策略选择已过期。", show_alert=True)
            return "preset_invalid"
        try:
            snapshot = await self._snapshot_reader.snapshot_for(session.source)
        except (RuntimeError, ValueError):
            await self._bot.edit(
                message_id=update.message_id,
                text=_render_snapshot_unavailable(session.source),
                keyboard=_strategy_keyboard(session.session_id),
            )
            await self._bot.answer(
                update.callback_query_id,
                text="账户或行情快照暂不可用；交互消息已保留重试入口。",
                show_alert=True,
            )
            return "account_snapshot_unavailable"
        if snapshot.account_ref != self._config.account_ref or snapshot.venue != self._config.venue:
            await self._bot.answer(update.callback_query_id, text="账户快照与手动账户不匹配。", show_alert=True)
            return "account_snapshot_mismatch"
        recommendation = recommend_manual_trade(
            account_equity=snapshot.account_equity_usd,
            config=self._config.presets[preset],
        )
        stored = await self._store_preview(
            update=update,
            session=session,
            preset=preset,
            snapshot=snapshot,
            recommended=recommendation.parameters,
            selected=recommendation.parameters,
        )
        if stored is None:
            await self._bot.answer(update.callback_query_id, text="交易会话已变化，请重新打开。", show_alert=True)
            return "preview_conflict"
        await self._render_session(update, stored)
        return "preview_ready"

    async def _adjust(
        self,
        update: TelegramTradingUpdate,
        session: ManualTradeSession,
        argument: str,
    ) -> str:
        if (
            session.preset is None
            or session.account_snapshot is None
            or session.recommended is None
            or session.selected is None
            or session.state
            not in {
                ManualSessionState.PREVIEW,
                ManualSessionState.MODIFYING,
                ManualSessionState.HIGH_RISK_CONFIRMATION,
            }
        ):
            await self._bot.answer(update.callback_query_id, text="当前不能修改参数。", show_alert=True)
            return "modification_invalid"
        selected = _adjust_parameters(session.selected, argument)
        stored = await self._store_preview(
            update=update,
            session=session,
            preset=session.preset,
            snapshot=session.account_snapshot,
            recommended=session.recommended,
            selected=selected,
        )
        if stored is None:
            await self._bot.answer(update.callback_query_id, text="参数更新冲突，请重试。", show_alert=True)
            return "modification_conflict"
        await self._render_session(update, stored)
        return "modification_applied"

    async def _store_preview(
        self,
        *,
        update: TelegramTradingUpdate,
        session: ManualTradeSession,
        preset: StrategyPreset,
        snapshot: ManualAccountSnapshot,
        recommended: ManualTradeParameters,
        selected: ManualTradeParameters,
    ) -> ManualTradeSession | None:
        if snapshot.venue != self._config.venue:
            return None
        preview = build_manual_trade_preview(
            side=session.source.side,
            venue=self._config.venue,
            account_equity=snapshot.account_equity_usd,
            reference_entry=snapshot.reference_entry,
            parameters=selected,
            liquidation_distance_bps=snapshot.liquidation_distance_bps,
        )
        guard = guard_manual_trade_modification(
            preset=preset,
            account_equity=snapshot.account_equity_usd,
            recommended=recommended,
            modified=selected,
            config=self._config.risk,
        )
        if is_development_test_source(session.source) and selected.notional_usd > MAX_DEVELOPMENT_TEST_NOTIONAL_USD:
            guard = guard.model_copy(
                update={
                    "state": ModificationGuardState.REJECTED,
                    "reason_codes": tuple((*guard.reason_codes, "development_test_notional_cap")),
                }
            )
        return await self._repository.set_preview(
            session_id=session.session_id,
            preset=preset,
            account_snapshot=snapshot,
            recommended=recommended,
            selected=selected,
            preview=preview,
            guard=guard,
            update_id=update.update_id,
            result_code="preview_ready" if session.preset is None else "modification_applied",
            now_ms=int(self._clock_ms()),
        )

    async def _render_session(self, update: TelegramTradingUpdate, session: ManualTradeSession) -> None:
        if session.preview is None or session.guard is None:
            raise RuntimeError("manual_trading_preview_invariant")
        await self._bot.edit(
            message_id=update.message_id,
            text=_render_preview(session),
            keyboard=_preview_keyboard(session),
        )
        await self._bot.answer(update.callback_query_id, text="交易预览已更新。")

    async def _confirm(
        self,
        update: TelegramTradingUpdate,
        session: ManualTradeSession,
        *,
        high_risk: bool,
    ) -> str:
        expected = ManualSessionState.HIGH_RISK_CONFIRMATION if high_risk else ManualSessionState.PREVIEW
        if (
            session.state is not expected
            or session.preset is None
            or session.account_snapshot is None
            or session.recommended is None
            or session.selected is None
            or session.guard is None
            or session.account_snapshot.venue != self._config.venue
        ):
            await self._bot.answer(update.callback_query_id, text="确认状态不匹配，请重新检查预览。", show_alert=True)
            return "confirmation_invalid"
        now_ms = int(self._clock_ms())
        intent = create_manual_trade_intent(
            session_id=session.session_id,
            source=session.source,
            actor_user_id=session.actor_user_id,
            account_ref=session.account_snapshot.account_ref,
            venue=self._config.venue,
            preset=session.preset,
            recommended=session.recommended,
            selected=session.selected,
            reference_entry=session.account_snapshot.reference_entry,
            account_equity=session.account_snapshot.account_equity_usd,
            guard=session.guard,
            confirmed_at_ms=now_ms,
            high_risk_confirmed_at_ms=now_ms if high_risk else None,
        )
        if not await self._repository.confirm_intent(
            intent,
            update_id=update.update_id,
            result_code="intent_confirmed",
            now_ms=now_ms,
        ):
            await self._bot.answer(update.callback_query_id, text="该交易已确认或状态已变化。", show_alert=True)
            return "confirmation_conflict"
        await self._bot.edit(
            message_id=update.message_id,
            text=_render_execution_state(
                session.model_copy(update={"state": ManualSessionState.CONFIRMED, "intent_id": intent.intent_id})
            ),
            keyboard=(("我的交易", "tf:mine:v1"),),
        )
        await self._bot.answer(update.callback_query_id, text="交易已确认。")
        return "intent_confirmed"

    async def _show_my_trades(self, update: TelegramTradingUpdate) -> str:
        sessions = await self._repository.list_sessions(
            actor_user_id=update.actor_user_id,
            chat_id=update.chat_id,
        )
        if not sessions:
            text = "📭 <b>我的交易</b>\n\n暂无手动交易记录。"
        else:
            lines = ["📋 <b>我的交易</b>", ""]
            for session in sessions:
                lines.append(
                    f"• {html.escape(session.source.base_symbol)} {_side_zh(session.source.side)} — "
                    f"{_session_state_zh(session.state)}"
                )
                if session.intent_id is not None:
                    lines.append(f"  <code>{session.intent_id[:12]}</code>")
            text = "\n".join(lines)
        await self._bot.edit(
            message_id=update.message_id,
            text=text,
            keyboard=(("刷新", "tf:mine:v1"),),
        )
        await self._bot.answer(update.callback_query_id, text="交易状态已刷新。")
        return "my_trades_shown"

    async def _cancel(self, update: TelegramTradingUpdate, session: ManualTradeSession) -> str:
        if session.state in {
            ManualSessionState.CONFIRMED,
            ManualSessionState.SUBMITTING,
            ManualSessionState.OPEN,
            ManualSessionState.AMBIGUOUS,
            ManualSessionState.CLOSED,
        }:
            await self._bot.answer(update.callback_query_id, text="交易已进入执行流程，不能在此取消。", show_alert=True)
            return "cancel_forbidden"
        if not await self._repository.cancel_session(
            session.session_id,
            update_id=update.update_id,
            result_code="cancelled",
            now_ms=int(self._clock_ms()),
        ):
            return "cancel_conflict"
        await self._bot.edit(
            message_id=update.message_id,
            text="已取消本次交易。",
            keyboard=(("我的交易", "tf:mine:v1"),),
        )
        await self._bot.answer(update.callback_query_id, text="已取消。")
        return "cancelled"

    async def _render_replayed_effect(
        self,
        update: TelegramTradingUpdate,
        session: ManualTradeSession,
    ) -> None:
        if session.state in {
            ManualSessionState.PREVIEW,
            ManualSessionState.MODIFYING,
            ManualSessionState.HIGH_RISK_CONFIRMATION,
        }:
            await self._render_session(update, session)
            return
        if session.state in {
            ManualSessionState.CONFIRMED,
            ManualSessionState.SUBMITTING,
            ManualSessionState.OPEN,
            ManualSessionState.AMBIGUOUS,
        }:
            await self._bot.edit(
                message_id=update.message_id,
                text=_render_execution_state(session),
                keyboard=(("我的交易", "tf:mine:v1"),),
            )
            await self._bot.answer(update.callback_query_id, text="交易状态已恢复。")
            return
        if session.state is ManualSessionState.CANCELLED:
            await self._bot.edit(
                message_id=update.message_id,
                text="已取消本次交易。",
                keyboard=(("我的交易", "tf:mine:v1"),),
            )
            await self._bot.answer(update.callback_query_id, text="已取消。")
            return
        await self._bot.answer(update.callback_query_id, text="操作已处理。")


def _parse_session_action(data: str) -> tuple[str, str | None, str] | None:
    parts = data.split(":")
    try:
        if len(parts) == 4 and parts[:2] == ["tf", "p"]:
            return "preset", parts[2], _canonical_session_id(parts[3])
        if len(parts) == 3 and parts[0] == "tf" and parts[1] in {"m", "b", "c", "h", "x"}:
            action = {
                "m": "modify",
                "b": "back",
                "c": "confirm",
                "h": "high_risk_confirm",
                "x": "cancel",
            }[parts[1]]
            return action, None, _canonical_session_id(parts[2])
        if len(parts) == 5 and parts[:2] == ["tf", "a"] and parts[2] in {"n", "s", "t"}:
            if parts[3] not in {"u", "d"}:
                return None
            return "adjust", f"{parts[2]}:{parts[3]}", _canonical_session_id(parts[4])
    except ValueError:
        return None
    return None


def _parse_portfolio_action(data: str) -> tuple[str, str | None, str | None] | None:
    if data == "tf:mine:v1":
        return "menu", None, None
    if data in {"tf:mine:open", "tf:mine:closed", "tf:mine:events"}:
        return data.rsplit(":", maxsplit=1)[-1], None, None
    parts = data.split(":")
    try:
        if len(parts) == 3 and parts[:2] == ["tf", "pos"]:
            return "position", None, _canonical_session_id(parts[2])
        if len(parts) == 3 and parts[:2] == ["tf", "news"]:
            return "news", None, _canonical_session_id(parts[2])
        if len(parts) == 3 and parts[:2] == ["tf", "close"]:
            return "close", None, _canonical_session_id(parts[2])
        if len(parts) == 4 and parts[:2] == ["tf", "closep"]:
            _close_bps(parts[2])
            return "close_preview", parts[2], _canonical_session_id(parts[3])
        if len(parts) == 4 and parts[:2] == ["tf", "closec"]:
            _close_bps(parts[2])
            return "close_confirm", parts[2], _canonical_session_id(parts[3])
    except ValueError:
        return None
    return None


def _close_bps(value: str | None) -> int:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError("manual_close_fraction_invalid") from exc
    if parsed not in {3000, 5000, 10000}:
        raise ValueError("manual_close_fraction_invalid")
    return parsed


def _parse_target_action(data: str) -> tuple[str, str] | None:
    parts = data.split(":")
    if len(parts) != 4 or parts[:2] != ["tf", "t"]:
        return None
    symbol = parts[2]
    if re.fullmatch(r"[A-Z0-9][A-Z0-9.-]{0,19}", symbol) is None:
        return None
    try:
        picker_id = _canonical_session_id(parts[3])
    except ValueError:
        return None
    return symbol, picker_id


def _canonical_session_id(value: str) -> str:
    parsed = UUID(str(value))
    canonical = str(parsed)
    if canonical != value:
        raise ValueError("manual_trade_session_id_noncanonical")
    return canonical


def _session_bound_to_update(session: ManualTradeSession | None, update: TelegramTradingUpdate) -> bool:
    return bool(
        session is not None
        and session.actor_user_id == update.actor_user_id
        and session.chat_id == update.chat_id
        and session.interaction_message_id == update.message_id
    )


def _picker_bound_to_update(picker: ManualTargetPicker | None, update: TelegramTradingUpdate) -> bool:
    return bool(
        picker is not None
        and picker.state in {ManualTargetPickerState.SENT, ManualTargetPickerState.CONSUMED}
        and picker.actor_user_id == update.actor_user_id
        and picker.chat_id == update.chat_id
        and picker.interaction_message_id == update.message_id
    )


def _portfolio_menu_keyboard() -> tuple[tuple[str, str], ...]:
    return (
        ("当前持仓", "tf:mine:open"),
        ("历史持仓", "tf:mine:closed"),
        ("交易记录", "tf:mine:events"),
    )


def _position_list_keyboard(
    positions: tuple[ManualPositionView, ...],
    *,
    closed: bool,
) -> tuple[tuple[str, str], ...]:
    buttons = [
        (
            f"{position.source.base_symbol} {'历史' if closed else '持仓'}",
            f"tf:pos:{position.session_id}",
        )
        for position in positions[:8]
    ]
    buttons.extend((("刷新", "tf:mine:closed" if closed else "tf:mine:open"), ("返回", "tf:mine:v1")))
    return tuple(buttons)


def _position_keyboard(position: ManualPositionView) -> tuple[tuple[str, str], ...]:
    buttons: list[tuple[str, str]] = [("查看原新闻", f"tf:news:{position.session_id}")]
    close_pending = position.active_close is not None and position.active_close.state.value in {
        "PENDING",
        "SUBMITTING",
        "AMBIGUOUS",
    }
    if (
        position.state in {ManualPositionState.OPEN, ManualPositionState.EXPOSED}
        and position.quantity > 0
        and not close_pending
    ):
        buttons.append(("平仓", f"tf:close:{position.session_id}"))
    buttons.extend((("当前持仓", "tf:mine:open"), ("历史持仓", "tf:mine:closed"), ("交易记录", "tf:mine:events")))
    return tuple(buttons)


def _close_selector_keyboard(session_id: str) -> tuple[tuple[str, str], ...]:
    return (
        ("平仓 30%", f"tf:closep:3000:{session_id}"),
        ("平仓 50%", f"tf:closep:5000:{session_id}"),
        ("全部平仓", f"tf:closep:10000:{session_id}"),
        ("返回持仓", f"tf:pos:{session_id}"),
    )


def _close_confirmation_keyboard(session_id: str, *, requested_bps: int) -> tuple[tuple[str, str], ...]:
    return (
        (f"确认平仓 {_pct(requested_bps)}", f"tf:closec:{requested_bps}:{session_id}"),
        ("返回选择", f"tf:close:{session_id}"),
    )


def _render_position_list(positions: tuple[ManualPositionView, ...], *, closed: bool) -> str:
    title = "历史持仓" if closed else "当前持仓"
    if not positions:
        return f"📭 <b>{title}</b>\n\n暂无记录。"
    lines = [f"📊 <b>{title}</b>", ""]
    for position in positions:
        if closed:
            pnl = position.realized_pnl_usd
            pnl_text = "暂无" if pnl is None else f"{pnl:+.2f} USDT"
            lines.append(
                f"• <b>{html.escape(position.source.base_symbol)}</b> {_side_zh(position.side)} · "
                f"已平仓 · {pnl_text} · {_format_time(position.closed_at_ms)}"
            )
        else:
            lines.append(
                f"• <b>{html.escape(position.source.base_symbol)}</b> {_side_zh(position.side)} · "
                f"{position.quantity} · {position.unrealized_pnl_usd:+.2f} USDT "
                f"({_signed_pct(position.pnl_bps)}) · {_position_state_zh(position.state)}"
            )
    lines.append("\n点击下方标的查看完整数据。")
    return "\n".join(lines)


def _render_position_detail(position: ManualPositionView) -> str:
    tp_distance = _distance_bps(position.mark_price, position.take_profit_price)
    sl_distance = _distance_bps(position.mark_price, position.stop_loss_price)
    liquidation = "暂无" if position.liquidation_price is None else f"${position.liquidation_price:.6f}"
    strategy = "大仓位 / 小止损" if position.preset is StrategyPreset.TIGHT_STOP else "小仓位 / 大止损"
    notional = position.quantity * position.mark_price
    margin = notional / Decimal(position.leverage)
    lines = [
        f"📍 <b>{html.escape(position.source.base_symbol)} 持仓详情</b>",
        "",
        f"方向：{_side_zh(position.side)}",
        "交易市场：币安 U 本位合约（正式站）",
        f"状态：{_position_state_zh(position.state)}",
        f"持仓数量：{position.quantity} {html.escape(position.source.base_symbol)}",
        f"当前名义价值：${notional:.2f}",
        f"占用保证金估算：${margin:.2f}",
        f"杠杆倍数：{position.leverage}x",
        f"入场均价：${position.entry_price:.6f}",
        f"当前标记价：${position.mark_price:.6f}",
        f"未实现盈亏：{position.unrealized_pnl_usd:+.4f} USDT",
        f"持仓收益率：{_signed_pct(position.pnl_bps)} · 保证金回报率：{_signed_pct(position.margin_return_bps)}",
        f"止盈价：${position.take_profit_price:.6f}（距离 {_pct(tp_distance)}）",
        f"止损价：${position.stop_loss_price:.6f}（距离 {_pct(sl_distance)}）",
        f"强平价：{liquidation}",
        f"开仓时间：{_format_time(position.opened_at_ms)}",
        f"数据时间：{_format_time(position.observed_at_ms)}",
        f"风险结构：{strategy}",
        (
            "原推荐："
            f"{position.recommended.notional_usd:.2f}U / {position.recommended.leverage}x / "
            f"止损 {_pct(position.recommended.stop_loss_bps)} / 止盈 {_pct(position.recommended.take_profit_bps)}"
        ),
        (
            "最终参数："
            f"{position.selected.notional_usd:.2f}U / {position.selected.leverage}x / "
            f"止损 {_pct(position.selected.stop_loss_bps)} / 止盈 {_pct(position.selected.take_profit_bps)}"
        ),
        f"关联新闻：{html.escape(position.source.headline_zh)}",
    ]
    if position.state is ManualPositionState.CLOSED:
        lines.extend(
            [
                "",
                f"平仓时间：{_format_time(position.closed_at_ms)}",
                f"退出方式：{_exit_reason_zh(position.exit_reason)}",
                f"退出价格：{'暂无' if position.exit_price is None else f'${position.exit_price:.6f}'}",
                (
                    "已实现盈亏：暂无"
                    if position.realized_pnl_usd is None
                    else f"已实现盈亏：{position.realized_pnl_usd:+.4f} USDT"
                ),
            ]
        )
    if position.active_close is not None and position.active_close.state.value not in {"FILLED", "REJECTED"}:
        lines.extend(
            [
                "",
                f"平仓请求：{_pct(position.active_close.requested_bps)} · {position.active_close.state.value}",
            ]
        )
    return "\n".join(lines)


def _render_close_selector(position: ManualPositionView) -> str:
    return (
        f"⚠️ <b>选择 {html.escape(position.source.base_symbol)} 平仓比例</b>\n\n"
        f"当前数量：{position.quantity}\n"
        f"当前标记价：${position.mark_price:.6f}\n"
        f"未实现盈亏：{position.unrealized_pnl_usd:+.4f} USDT\n\n"
        "减仓使用 reduce-only 市价单；剩余仓位继续由原 close-position 止盈/止损保护。"
    )


def _render_close_confirmation(position: ManualPositionView, *, requested_bps: int) -> str:
    estimated_quantity = position.quantity * Decimal(requested_bps) / Decimal(10_000)
    estimated_notional = estimated_quantity * position.mark_price
    return (
        "⚠️ <b>确认真实平仓</b>\n\n"
        f"标的：{html.escape(position.source.base_symbol)} {_side_zh(position.side)}\n"
        f"比例：{_pct(requested_bps)}\n"
        f"预计数量：{estimated_quantity}\n"
        f"预计名义价值：${estimated_notional:.2f}\n"
        "委托：币安正式站 reduce-only 市价单\n\n"
        "最终成交数量和价格以交易所回报为准。"
    )


def _render_trade_events(events: tuple[ManualTradeHistoryEvent, ...]) -> str:
    if not events:
        return "📭 <b>交易记录</b>\n\n暂无记录。"
    lines = ["🧾 <b>交易记录</b>", ""]
    for event in events:
        leg = str(event.payload.get("leg") or "")
        details = _trade_event_details(event.payload)
        suffix = f" · {_trade_leg_zh(leg)}" if leg else ""
        lines.append(
            f"• {_format_time(event.created_at_ms)} · <b>{html.escape(event.symbol)}</b> · "
            f"{_event_kind_zh(event.event_kind)}{suffix}{details}"
        )
    return "\n".join(lines)


def _trade_event_details(payload: dict[str, object]) -> str:
    receipt = payload.get("receipt")
    if isinstance(receipt, dict):
        quantity = receipt.get("executed_quantity")
        price = receipt.get("average_price")
        status = receipt.get("status")
        values = [
            f"数量 {html.escape(str(quantity))}" if quantity not in {None, "0", 0} else "",
            f"价格 ${html.escape(str(price))}" if price not in {None, "0", 0} else "",
            f"状态 {html.escape(str(status))}" if status else "",
        ]
        detail = " / ".join(value for value in values if value)
        return f" · {detail}" if detail else ""
    if payload.get("remaining_quantity") is not None:
        return f" · 剩余 {html.escape(str(payload['remaining_quantity']))}"
    if payload.get("error_code") is not None:
        return f" · {html.escape(str(payload['error_code']))}"
    if payload.get("realized_pnl_usd") is not None:
        return f" · 已实现 {html.escape(str(payload['realized_pnl_usd']))} USDT"
    return ""


def _distance_bps(value: Decimal, target: Decimal) -> int:
    if value <= 0:
        return 0
    return int((abs(target - value) / value * Decimal(10_000)).to_integral_value())


def _signed_pct(value_bps: int) -> str:
    return f"{Decimal(value_bps) / Decimal(100):+.2f}%"


def _format_time(value_ms: int | None) -> str:
    if value_ms is None:
        return "暂无"
    return (
        datetime.fromtimestamp(value_ms / 1_000, tz=UTC)
        .astimezone(ZoneInfo("Asia/Shanghai"))
        .strftime("%m-%d %H:%M:%S")
    )


def _position_state_zh(state: ManualPositionState) -> str:
    return {
        ManualPositionState.OPEN: "持仓中",
        ManualPositionState.EXPOSED: "保护异常，需立即处理",
        ManualPositionState.CLOSING: "平仓对账中",
        ManualPositionState.CLOSED: "已平仓",
        ManualPositionState.MANUAL_REVIEW: "需人工核对",
    }[state]


def _event_kind_zh(kind: str) -> str:
    return {
        "SESSION_CREATED": "创建交易会话",
        "STRATEGY_SELECTED": "选择风险结构",
        "TRADE_MODIFIED": "修改交易参数",
        "HIGH_RISK_ACKNOWLEDGED": "确认高风险参数",
        "TRADE_CONFIRMED": "确认交易",
        "TRADE_CANCELLED": "取消交易",
        "ORDER_FENCED": "冻结订单参数",
        "ORDER_SUBMITTED": "订单已成交/接受",
        "ORDER_REJECTED": "订单被拒绝",
        "PROTECTION_REJECTED": "保护单失败",
        "ORDER_AMBIGUOUS": "订单结果待核对",
        "ORDER_RECONCILED": "订单已对账",
        "POSITION_OPENED": "仓位已打开",
        "TP_CREATED": "止盈单已创建",
        "SL_CREATED": "止损单已创建",
        "POSITION_CLOSED": "仓位已关闭",
    }.get(kind, kind)


def _trade_leg_zh(leg: str) -> str:
    return {
        "entry": "入场",
        "take_profit": "止盈",
        "stop_loss": "止损",
        "execution_setting": "杠杆设置",
        "manual_close": "手动平仓",
    }.get(leg, leg)


def _exit_reason_zh(reason: str | None) -> str:
    if reason is None:
        return "暂无"
    return {
        "manual_close": "手动平仓",
        "take_profit": "止盈触发",
        "stop_loss": "止损触发",
        "protection_or_external_close": "止盈 / 止损或交易所侧平仓",
    }.get(reason, reason)


def _strategy_keyboard(session_id: str) -> tuple[tuple[str, str], ...]:
    return (
        ("大仓位 / 小止损", f"tf:p:t:{session_id}"),
        ("小仓位 / 大止损", f"tf:p:w:{session_id}"),
        ("取消", f"tf:x:{session_id}"),
    )


def _target_keyboard(
    sources: tuple[ManualTradeSource, ...],
    *,
    picker_id: str,
) -> tuple[tuple[str, str], ...]:
    return tuple((source.base_symbol, f"tf:t:{source.base_symbol}:{picker_id}") for source in sources)


def _preview_keyboard(session: ManualTradeSession) -> tuple[tuple[str, str], ...]:
    if session.guard is None:
        return (("取消", f"tf:x:{session.session_id}"),)
    if session.guard.state is ModificationGuardState.HIGH_RISK_CONFIRMATION:
        return (
            ("仍然执行", f"tf:h:{session.session_id}"),
            ("返回修改", f"tf:m:{session.session_id}"),
            ("取消", f"tf:x:{session.session_id}"),
        )
    if session.guard.state is ModificationGuardState.REJECTED:
        return (("返回修改", f"tf:m:{session.session_id}"), ("取消", f"tf:x:{session.session_id}"))
    return (
        ("确认交易", f"tf:c:{session.session_id}"),
        ("修改", f"tf:m:{session.session_id}"),
        ("取消", f"tf:x:{session.session_id}"),
    )


def _modification_keyboard(session_id: str) -> tuple[tuple[str, str], ...]:
    return (
        ("仓位 -50%", f"tf:a:n:d:{session_id}"),
        ("仓位 +50%", f"tf:a:n:u:{session_id}"),
        ("止损 -50%", f"tf:a:s:d:{session_id}"),
        ("止损 +50%", f"tf:a:s:u:{session_id}"),
        ("止盈 -50%", f"tf:a:t:d:{session_id}"),
        ("止盈 +50%", f"tf:a:t:u:{session_id}"),
        ("返回预览", f"tf:b:{session_id}"),
        ("取消", f"tf:x:{session_id}"),
    )


def _adjust_parameters(parameters: ManualTradeParameters, argument: str) -> ManualTradeParameters:
    field, direction = argument.split(":", maxsplit=1)
    factor = Decimal("1.5") if direction == "u" else Decimal("0.5")
    notional = parameters.notional_usd
    stop_loss_bps = parameters.stop_loss_bps
    take_profit_bps = parameters.take_profit_bps
    if field == "n":
        notional = (notional * factor).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    elif field == "s":
        stop_loss_bps = max(1, int(Decimal(stop_loss_bps) * factor))
    elif field == "t":
        take_profit_bps = max(1, int(Decimal(take_profit_bps) * factor))
    else:
        raise ValueError("manual_trade_modification_field_invalid")
    return ManualTradeParameters(
        notional_usd=notional,
        leverage=parameters.leverage,
        stop_loss_bps=stop_loss_bps,
        take_profit_bps=take_profit_bps,
    )


def _render_strategy_selector(source: ManualTradeSource) -> str:
    return (
        f"🎯 <b>{html.escape(source.base_symbol)} {_side_zh(source.side)}</b>\n\n"
        f"来源新闻: {html.escape(source.headline_zh)}\n\n"
        "请选择风险结构；系统会基于最新账户权益与参考价格生成完整预览。"
    )


def _render_snapshot_unavailable(source: ManualTradeSource) -> str:
    return (
        "⚠️ <b>交易预览暂不可用</b>\n\n"
        f"{html.escape(source.base_symbol)} {_side_zh(source.side)}\n"
        "账户或行情快照暂不可用，请稍后重新选择风险结构。"
    )


def _render_target_selector() -> str:
    return "🎯 <b>选择交易标的</b>\n\n这条 TG 新闻展示了多个标的，请先选择本次要交易的标的。"


def _render_preview(session: ManualTradeSession) -> str:
    if session.preview is None or session.preset is None or session.guard is None:
        raise RuntimeError("manual_trading_preview_invariant")
    preview = session.preview
    preset_label = "大仓位 / 小止损" if session.preset is StrategyPreset.TIGHT_STOP else "小仓位 / 大止损"
    liquidation = (
        f"{_pct(preview.liquidation_distance_bps)}"
        if preview.liquidation_distance_bps is not None
        else "暂无权威值（成交前）"
    )
    lines = [
        f"<b>{html.escape(session.source.base_symbol)} {_side_zh(session.source.side)}</b>",
        "交易市场：币安 U 本位合约（正式站）",
        f"账户权益：${preview.account_equity_usd:.2f}",
        f"风险结构：{preset_label}",
        "委托类型：市价",
        f"名义仓位：${preview.parameters.notional_usd:.2f}",
        f"杠杆倍数：{preview.parameters.leverage}x",
        f"占用保证金：${preview.margin_usd:.2f}",
        f"参考入场价：${preview.reference_entry:.2f}",
        "",
        f"止损价：${preview.stop_loss_price:.2f}（-{_pct(preview.parameters.stop_loss_bps)}）",
        f"预计亏损：-${preview.estimated_loss_usd:.2f}",
        f"账户风险：-{_pct(preview.account_risk_bps)}",
        "",
        f"止盈价：${preview.take_profit_price:.2f}（+{_pct(preview.parameters.take_profit_bps)}）",
        f"预计盈利：+${preview.estimated_profit_usd:.2f}",
        f"账户潜在收益：+{_pct(preview.potential_account_return_bps)}",
        f"距强平价：{liquidation}",
    ]
    if session.guard.state is ModificationGuardState.HIGH_RISK_CONFIRMATION:
        if session.recommended is None or session.selected is None:
            raise RuntimeError("manual_trading_high_risk_comparison_invariant")
        lines.extend(
            [
                "",
                "⚠️ <b>高风险二次确认</b>",
                (
                    f"原最大亏损 ${session.guard.original_max_loss_usd:.2f} → "
                    f"新最大亏损 ${session.guard.modified_max_loss_usd:.2f}"
                ),
                (
                    f"名义仓位 ${session.recommended.notional_usd:.2f} → ${session.selected.notional_usd:.2f} "
                    f"（偏离 {_pct(session.guard.notional_deviation_bps)}）"
                ),
                (
                    f"止损 {_pct(session.recommended.stop_loss_bps)} → {_pct(session.selected.stop_loss_bps)} "
                    f"（偏离 {_pct(session.guard.stop_loss_deviation_bps)}）"
                ),
                (
                    f"止盈 {_pct(session.recommended.take_profit_bps)} → {_pct(session.selected.take_profit_bps)} "
                    f"（偏离 {_pct(session.guard.take_profit_deviation_bps)}）"
                ),
                f"新账户风险：{_pct(session.guard.modified_account_risk_bps)}",
                "原因：" + "、".join(_guard_reason_zh(code) for code in session.guard.reason_codes),
            ]
        )
    elif session.guard.state is ModificationGuardState.REJECTED:
        lines.extend(
            [
                "",
                "⛔ <b>参数不可执行</b>",
                "原因：" + "、".join(_guard_reason_zh(code) for code in session.guard.reason_codes),
            ]
        )
    return "\n".join(lines)


def _render_execution_state(session: ManualTradeSession) -> str:
    icon, detail = {
        ManualSessionState.CONFIRMED: ("⏳", "交易已确认，等待正式账户执行器领取。"),
        ManualSessionState.SUBMITTING: ("⏳", "订单正在提交或对账，系统不会盲目重试。"),
        ManualSessionState.OPEN: ("✅", "入场成交且止盈 / 止损保护单已被正式账户接受。"),
        ManualSessionState.AMBIGUOUS: ("⚠️", "订单结果不明确，已冻结写入并进入只读对账。"),
    }.get(session.state, ("ℹ️", f"当前状态：{_session_state_zh(session.state)}"))
    intent = f"\n交易意图 ID：<code>{session.intent_id[:12]}</code>" if session.intent_id else ""
    return (
        f"{icon} <b>{html.escape(session.source.base_symbol)} {_side_zh(session.source.side)}</b>\n\n{detail}{intent}"
    )


def _render_modification_menu(session: ManualTradeSession) -> str:
    if session.selected is None:
        return "当前没有可修改的交易预览。"
    return (
        f"✏️ <b>修改 {html.escape(session.source.base_symbol)} 参数</b>\n\n"
        f"名义仓位：${session.selected.notional_usd:.2f}\n"
        f"止损：{_pct(session.selected.stop_loss_bps)}\n"
        f"止盈：{_pct(session.selected.take_profit_bps)}\n\n"
        "每次修改都会重新计算组合最大亏损与账户风险。"
    )


def _side_zh(side: TradeSide) -> str:
    return {TradeSide.LONG: "做多", TradeSide.SHORT: "做空"}[side]


def _guard_reason_zh(code: str) -> str:
    return {
        "leverage_out_of_range": "杠杆超出允许范围",
        "insufficient_margin": "保证金不足",
        "notional_deviation": "名义仓位偏离建议值",
        "stop_loss_deviation": "止损偏离建议值",
        "take_profit_deviation": "止盈偏离建议值",
        "combined_max_loss": "组合最大亏损过高",
        "account_risk": "账户风险过高",
        "development_test_notional_cap": "测试新闻交易名义仓位不得超过 200U",
    }.get(code, "风险参数不符合要求")


def _pct(value_bps: int) -> str:
    return f"{Decimal(value_bps) / Decimal(100):.2f}%"


def _session_state_zh(state: ManualSessionState) -> str:
    return {
        ManualSessionState.AWAITING_STRATEGY: "待选策略",
        ManualSessionState.PREVIEW: "待确认",
        ManualSessionState.MODIFYING: "修改中",
        ManualSessionState.HIGH_RISK_CONFIRMATION: "待高风险确认",
        ManualSessionState.CONFIRMED: "等待执行",
        ManualSessionState.SUBMITTING: "执行中",
        ManualSessionState.OPEN: "持仓与保护单已确认",
        ManualSessionState.AMBIGUOUS: "等待对账",
        ManualSessionState.EXPOSED: "保护失败，需人工处理",
        ManualSessionState.REJECTED: "已拒绝",
        ManualSessionState.CANCELLED: "已取消",
        ManualSessionState.CLOSED: "已平仓",
    }[state]


__all__ = ["ManualTelegramTradingController", "ManualTradingControllerConfig"]
