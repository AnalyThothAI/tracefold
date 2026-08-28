"""App-owned Telegram -> News projection -> manual Trading composition seam."""

from __future__ import annotations

import html
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Protocol
from uuid import UUID, uuid4

from tracefold.integrations.telegram import TelegramTradingUpdate
from tracefold.trading import (
    ManualAccountSnapshot,
    ManualModificationGuard,
    ManualRiskConfig,
    ManualSessionState,
    ManualStrategyPresetConfig,
    ManualTradeIntent,
    ManualTradeParameters,
    ManualTradePreview,
    ManualTradeSession,
    ManualTradeSource,
    ManualVenue,
    ModificationGuardState,
    StrategyPreset,
    build_manual_trade_preview,
    create_manual_trade_intent,
    guard_manual_trade_modification,
    recommend_manual_trade,
)


class ManualTradingRepositoryPort(Protocol):
    async def source_for_message(self, message_id: int) -> ManualTradeSource | None: ...

    async def begin_session(
        self,
        *,
        session_id: str,
        source: ManualTradeSource,
        actor_user_id: int,
        chat_id: int,
        update_id: int,
        now_ms: int,
    ) -> tuple[ManualTradeSession, bool]: ...

    async def begin_interaction_reply(self, session_id: str, *, now_ms: int) -> bool: ...

    async def attach_interaction_message(self, session_id: str, *, message_id: int, now_ms: int) -> bool: ...

    async def get_session(self, session_id: str) -> ManualTradeSession | None: ...

    async def list_sessions(self, *, actor_user_id: int, chat_id: int) -> tuple[ManualTradeSession, ...]: ...

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
        if update.data == "tf:mine:v1":
            return await self._show_my_trades(update)
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

    async def _start_trade(self, update: TelegramTradingUpdate) -> str:
        source = await self._repository.source_for_message(update.message_id)
        if source is None:
            await self._bot.answer(update.callback_query_id, text="这条消息没有可交易的单一标的。", show_alert=True)
            return "source_unavailable"
        session, created = await self._repository.begin_session(
            session_id=_canonical_session_id(self._session_id_factory()),
            source=source,
            actor_user_id=update.actor_user_id,
            chat_id=update.chat_id,
            update_id=update.update_id,
            now_ms=int(self._clock_ms()),
        )
        if session.interaction_message_id is None:
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
        await self._bot.answer(update.callback_query_id, text="请选择交易策略。")
        return "session_created" if created else "session_resumed"

    async def _show_detail(self, update: TelegramTradingUpdate) -> str:
        source = await self._repository.source_for_message(update.message_id)
        if source is None:
            await self._bot.answer(update.callback_query_id, text="详细数据暂不可用。", show_alert=True)
            return "detail_unavailable"
        await self._bot.answer(
            update.callback_query_id,
            text=(
                f"{source.base_symbol} {source.side.value.upper()}：行情、关联复核和观点已在原新闻中原位补全；"
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
        except RuntimeError:
            await self._bot.answer(
                update.callback_query_id, text="账户或行情快照暂不可用，请稍后重试。", show_alert=True
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
        preview = build_manual_trade_preview(
            side=session.source.side,
            venue=snapshot.venue,
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
        ):
            await self._bot.answer(update.callback_query_id, text="确认状态不匹配，请重新检查预览。", show_alert=True)
            return "confirmation_invalid"
        now_ms = int(self._clock_ms())
        intent = create_manual_trade_intent(
            session_id=session.session_id,
            source=session.source,
            actor_user_id=session.actor_user_id,
            account_ref=session.account_snapshot.account_ref,
            venue=session.account_snapshot.venue,
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
                    f"• {html.escape(session.source.base_symbol)} {session.source.side.value.upper()} — "
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


def _strategy_keyboard(session_id: str) -> tuple[tuple[str, str], ...]:
    return (
        ("大仓位 / 小止损", f"tf:p:t:{session_id}"),
        ("小仓位 / 大止损", f"tf:p:w:{session_id}"),
        ("取消", f"tf:x:{session_id}"),
    )


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
        ("SL -50%", f"tf:a:s:d:{session_id}"),
        ("SL +50%", f"tf:a:s:u:{session_id}"),
        ("TP -50%", f"tf:a:t:d:{session_id}"),
        ("TP +50%", f"tf:a:t:u:{session_id}"),
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
        f"🎯 <b>{html.escape(source.base_symbol)} {source.side.value.upper()}</b>\n\n"
        f"来源新闻: {html.escape(source.headline_zh)}\n\n"
        "请选择风险结构；系统会基于最新账户权益与参考价格生成完整预览。"
    )


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
        f"<b>{html.escape(session.source.base_symbol)} {session.source.side.value.upper()}</b>",
        "Venue: Binance USD-M Demo",
        f"Account Equity: ${preview.account_equity_usd:.2f}",
        f"Strategy: {preset_label}",
        "Order: Market",
        f"Notional: ${preview.parameters.notional_usd:.2f}",
        f"Leverage: {preview.parameters.leverage}x",
        f"Margin: ${preview.margin_usd:.2f}",
        f"Reference Entry: ${preview.reference_entry:.2f}",
        "",
        f"Stop Loss: ${preview.stop_loss_price:.2f} (-{_pct(preview.parameters.stop_loss_bps)})",
        f"Estimated Loss: -${preview.estimated_loss_usd:.2f}",
        f"Account Risk: -{_pct(preview.account_risk_bps)}",
        "",
        f"Take Profit: ${preview.take_profit_price:.2f} (+{_pct(preview.parameters.take_profit_bps)})",
        f"Estimated Profit: +${preview.estimated_profit_usd:.2f}",
        f"Potential Account Return: +{_pct(preview.potential_account_return_bps)}",
        f"Liquidation Distance: {liquidation}",
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
                    f"Notional ${session.recommended.notional_usd:.2f} → ${session.selected.notional_usd:.2f} "
                    f"(偏离 {_pct(session.guard.notional_deviation_bps)})"
                ),
                (
                    f"SL {_pct(session.recommended.stop_loss_bps)} → {_pct(session.selected.stop_loss_bps)} "
                    f"(偏离 {_pct(session.guard.stop_loss_deviation_bps)})"
                ),
                (
                    f"TP {_pct(session.recommended.take_profit_bps)} → {_pct(session.selected.take_profit_bps)} "
                    f"(偏离 {_pct(session.guard.take_profit_deviation_bps)})"
                ),
                f"新 Account Risk: {_pct(session.guard.modified_account_risk_bps)}",
                "原因: " + ", ".join(session.guard.reason_codes),
            ]
        )
    elif session.guard.state is ModificationGuardState.REJECTED:
        lines.extend(["", "⛔ <b>参数不可执行</b>", "原因: " + ", ".join(session.guard.reason_codes)])
    return "\n".join(lines)


def _render_execution_state(session: ManualTradeSession) -> str:
    icon, detail = {
        ManualSessionState.CONFIRMED: ("⏳", "交易已确认，等待 Demo 执行 authority 领取。"),
        ManualSessionState.SUBMITTING: ("⏳", "订单正在提交或对账，系统不会盲目重试。"),
        ManualSessionState.OPEN: ("✅", "入场成交且 TP / SL 保护单已被 Demo 接受。"),
        ManualSessionState.AMBIGUOUS: ("⚠️", "订单结果不明确，已冻结写入并进入只读对账。"),
    }.get(session.state, ("ℹ️", f"当前状态：{_session_state_zh(session.state)}"))
    intent = f"\nIntent: <code>{session.intent_id[:12]}</code>" if session.intent_id else ""
    return (
        f"{icon} <b>{html.escape(session.source.base_symbol)} {session.source.side.value.upper()}</b>\n\n"
        f"{detail}{intent}"
    )


def _render_modification_menu(session: ManualTradeSession) -> str:
    if session.selected is None:
        return "当前没有可修改的交易预览。"
    return (
        f"✏️ <b>修改 {html.escape(session.source.base_symbol)} 参数</b>\n\n"
        f"Notional: ${session.selected.notional_usd:.2f}\n"
        f"SL: {_pct(session.selected.stop_loss_bps)}\n"
        f"TP: {_pct(session.selected.take_profit_bps)}\n\n"
        "每次修改都会重新计算组合最大亏损与账户风险。"
    )


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
