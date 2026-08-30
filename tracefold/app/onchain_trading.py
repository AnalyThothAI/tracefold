"""Telegram controller for independent read-only onchain route analysis."""

from __future__ import annotations

import html
import re
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol
from uuid import UUID, uuid4

from tracefold.integrations.telegram import TelegramTradingUpdate
from tracefold.trading import (
    OnchainAnalysisSession,
    OnchainAnalysisState,
    OnchainAssetCandidate,
    OnchainExecutionIntent,
    OnchainExecutionState,
    OnchainNewsSource,
    OnchainQuoteRequest,
    OnchainRouteAnalysis,
    OnchainRouteQuote,
    OnchainTelegramEditEffect,
    OnchainTelegramEditPayload,
    OnchainTelegramEditState,
    RouteAnalysisState,
    onchain_wallet_fingerprint,
)


@dataclass(frozen=True, slots=True)
class OnchainCandidateResult:
    candidates: tuple[OnchainAssetCandidate, ...]
    provider_errors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class OnchainQuoteResult:
    analysis: OnchainRouteAnalysis
    settlement_symbol: str
    settlement_decimals: int
    output_decimals: int
    provider_errors: tuple[str, ...] = ()


class OnchainTradingRepositoryPort(Protocol):
    async def sources_for_message(self, message_id: int) -> tuple[OnchainNewsSource, ...]: ...

    async def begin_session(
        self,
        *,
        session_id: str,
        sources: tuple[OnchainNewsSource, ...],
        selected_ticker: str | None,
        actor_user_id: int,
        chat_id: int,
        now_ms: int,
    ) -> tuple[OnchainAnalysisSession, bool]: ...

    async def begin_interaction_reply(self, session_id: str, *, now_ms: int) -> bool: ...

    async def attach_interaction_message(self, session_id: str, *, message_id: int, now_ms: int) -> bool: ...

    async def mark_interaction_reply_ambiguous(
        self,
        session_id: str,
        *,
        error_code: str,
        now_ms: int,
    ) -> bool: ...

    async def get_session(self, session_id: str) -> OnchainAnalysisSession | None: ...

    async def begin_execution(
        self,
        *,
        execution_id: str,
        session: OnchainAnalysisSession,
        provider: str,
        wallet_address: str,
        request: OnchainQuoteRequest,
        quote: OnchainRouteQuote,
        now_ms: int,
    ) -> tuple[OnchainExecutionIntent, bool]: ...

    async def execution_for_session(self, session_id: str) -> OnchainExecutionIntent | None: ...

    async def executor_available(self, *, wallet_fingerprint: str, now_ms: int) -> bool: ...

    async def confirm_execution(self, session_id: str, *, update_id: int, now_ms: int) -> bool: ...

    async def cancel_execution(self, session_id: str, *, update_id: int, now_ms: int) -> bool: ...

    async def begin_edit(
        self,
        session_id: str,
        *,
        update_id: int,
        payload: OnchainTelegramEditPayload,
        result_code: str,
        now_ms: int,
    ) -> OnchainTelegramEditEffect: ...

    async def begin_resolution(
        self,
        session_id: str,
        *,
        ticker: str,
        now_ms: int,
    ) -> OnchainAnalysisSession | None: ...

    async def set_candidates(
        self,
        session_id: str,
        *,
        candidates: tuple[OnchainAssetCandidate, ...],
        provider_errors: tuple[str, ...],
        now_ms: int,
    ) -> OnchainAnalysisSession | None: ...

    async def set_candidates_and_begin_edit(
        self,
        session_id: str,
        *,
        candidates: tuple[OnchainAssetCandidate, ...],
        provider_errors: tuple[str, ...],
        update_id: int,
        payload: OnchainTelegramEditPayload,
        result_code: str,
        now_ms: int,
    ) -> tuple[OnchainAnalysisSession, OnchainTelegramEditEffect] | None: ...

    async def begin_quote(
        self,
        session_id: str,
        *,
        candidate_index: int | None,
        now_ms: int,
    ) -> OnchainAnalysisSession | None: ...

    async def set_analysis(
        self,
        session_id: str,
        *,
        result: OnchainQuoteResult,
        now_ms: int,
    ) -> OnchainAnalysisSession | None: ...

    async def set_analysis_and_begin_edit(
        self,
        session_id: str,
        *,
        result: OnchainQuoteResult,
        update_id: int,
        payload: OnchainTelegramEditPayload,
        result_code: str,
        now_ms: int,
    ) -> tuple[OnchainAnalysisSession, OnchainTelegramEditEffect] | None: ...

    async def cancel(self, session_id: str, *, now_ms: int) -> bool: ...

    async def cancel_and_begin_edit(
        self,
        session_id: str,
        *,
        update_id: int,
        payload: OnchainTelegramEditPayload,
        result_code: str,
        now_ms: int,
    ) -> OnchainTelegramEditEffect | None: ...

    async def edit_effect(
        self,
        session_id: str,
        *,
        update_id: int,
    ) -> OnchainTelegramEditEffect | None: ...

    async def settle_edit_sent(self, session_id: str, *, update_id: int, now_ms: int) -> bool: ...

    async def settle_edit_ambiguous(
        self,
        session_id: str,
        *,
        update_id: int,
        error_code: str,
        now_ms: int,
    ) -> bool: ...


class OnchainRouteGatewayPort(Protocol):
    async def close(self) -> None: ...

    async def resolve(self, ticker: str) -> OnchainCandidateResult: ...

    async def quote(self, candidate: OnchainAssetCandidate) -> OnchainQuoteResult: ...


class OnchainTradingBotPort(Protocol):
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


class OnchainTelegramTradingController:
    """Resolve displayed tickers, compare routes, and confirm one isolated manual-wallet execution."""

    def __init__(
        self,
        *,
        repository: OnchainTradingRepositoryPort,
        gateway: OnchainRouteGatewayPort,
        bot: OnchainTradingBotPort,
        wallet_address: str | None = None,
        execution_assets: dict[int, tuple[str, int]] | None = None,
        execution_available: bool = False,
        executable_providers: tuple[str, ...] = (),
        clock_ms: Callable[[], int] | None = None,
        session_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._repository = repository
        self._gateway = gateway
        self._bot = bot
        self._wallet_address = wallet_address
        self._execution_assets = dict(execution_assets or {})
        self._execution_available = bool(execution_available)
        self._executable_providers = frozenset(executable_providers)
        self._executor_live = False
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._session_id_factory = session_id_factory or (lambda: str(uuid4()))

    async def close(self) -> None:
        await self._gateway.close()

    async def handle(self, update: TelegramTradingUpdate) -> str:
        if not update.authorized:
            await self._bot.answer(update.callback_query_id, text="你没有链上分析权限。", show_alert=True)
            return "onchain_unauthorized"
        await self._refresh_executor_live()
        if update.data == "tf:onchain:v1":
            return await self._start(update)
        action = _parse_action(update.data)
        if action is None:
            await self._bot.answer(update.callback_query_id, text="链上操作无效或已过期。", show_alert=True)
            return "onchain_action_invalid"
        kind, argument, session_id = action
        session = await self._repository.get_session(session_id)
        if not _session_bound(session, update):
            await self._bot.answer(update.callback_query_id, text="该操作不属于当前链上会话。", show_alert=True)
            return "onchain_session_binding_mismatch"
        if session is None:
            raise RuntimeError("onchain_session_binding_invariant")
        existing_effect = await self._repository.edit_effect(
            session.session_id,
            update_id=update.update_id,
        )
        if existing_effect is not None:
            return await self._deliver_edit_effect(update, existing_effect)
        if kind == "asset":
            if argument is None:
                raise RuntimeError("onchain_asset_argument_invariant")
            source_index = int(argument)
            if not 0 <= source_index < len(session.sources):
                await self._bot.answer(update.callback_query_id, text="该标的不在 TG 展示列表中。", show_alert=True)
                return "onchain_ticker_invalid"
            return await self._select_ticker(update, session, session.sources[source_index].ticker)
        if kind == "candidate":
            if argument is None:
                raise RuntimeError("onchain_candidate_argument_invariant")
            return await self._select_candidate(update, session, int(argument))
        if kind == "refresh":
            return await self._refresh(update, session)
        if kind == "prepare":
            return await self._prepare_execution(update, session)
        if kind == "confirm":
            return await self._confirm_execution(update, session)
        if kind == "status":
            return await self._execution_status(update, session)
        return await self._cancel(update, session)

    async def _start(self, update: TelegramTradingUpdate) -> str:
        sources = await self._repository.sources_for_message(update.message_id)
        if not sources:
            await self._bot.answer(
                update.callback_query_id,
                text="这条新闻没有可用的 TG 展示标的。",
                show_alert=True,
            )
            return "onchain_source_unavailable"
        selected_ticker = sources[0].ticker if len(sources) == 1 else None
        now_ms = int(self._clock_ms())
        session, created = await self._repository.begin_session(
            session_id=self._session_id_factory(),
            sources=sources,
            selected_ticker=selected_ticker,
            actor_user_id=update.actor_user_id,
            chat_id=update.chat_id,
            now_ms=now_ms,
        )
        if not created and session.interaction_message_id is not None:
            await self._bot.answer(update.callback_query_id, text="这条新闻已有链上分析会话。")
            return "onchain_session_exists"
        if selected_ticker is None:
            result_code = "onchain_ticker_selection_required"
        elif session.state is OnchainAnalysisState.RESOLVING:
            session = await self._resolve(session, selected_ticker)
            result_code = "onchain_candidates_ready" if session.candidates else "onchain_candidates_unavailable"
        else:
            result_code = "onchain_candidates_ready" if session.candidates else "onchain_candidates_unavailable"
        if not await self._repository.begin_interaction_reply(session.session_id, now_ms=int(self._clock_ms())):
            await self._repository.mark_interaction_reply_ambiguous(
                session.session_id,
                error_code="telegram_reply_result_ambiguous_after_replay",
                now_ms=int(self._clock_ms()),
            )
            await self._bot.answer(
                update.callback_query_id,
                text="交互消息发送结果不确定；为避免重复消息，本次不会重发。",
                show_alert=True,
            )
            return "onchain_reply_ambiguous"
        try:
            message_id = await self._bot.reply(
                source_message_id=update.message_id,
                text=_render_session(session),
                keyboard=_session_keyboard(session, execution_available=self._can_execute(session)),
            )
        except Exception:
            marked = await self._repository.mark_interaction_reply_ambiguous(
                session.session_id,
                error_code="telegram_reply_result_ambiguous",
                now_ms=int(self._clock_ms()),
            )
            if not marked:
                raise RuntimeError("onchain_interaction_reply_ambiguity_conflict") from None
            with suppress(Exception):
                await self._bot.answer(
                    update.callback_query_id,
                    text="交互消息发送结果不确定；为避免重复消息，本次不会重发。",
                    show_alert=True,
                )
            return "onchain_reply_ambiguous"
        if not await self._repository.attach_interaction_message(
            session.session_id,
            message_id=message_id,
            now_ms=int(self._clock_ms()),
        ):
            raise RuntimeError("onchain_interaction_message_conflict")
        await self._bot.answer(update.callback_query_id, text="链上分析已打开。")
        return result_code

    async def _select_ticker(
        self,
        update: TelegramTradingUpdate,
        session: OnchainAnalysisSession,
        ticker: str,
    ) -> str:
        resolving = await self._repository.begin_resolution(
            session.session_id,
            ticker=ticker,
            now_ms=int(self._clock_ms()),
        )
        if resolving is None:
            await self._bot.answer(update.callback_query_id, text="该标的不在 TG 展示列表中。", show_alert=True)
            return "onchain_ticker_invalid"
        result, resolved = await self._resolved_projection(resolving, ticker)
        result_code = "onchain_candidates_ready" if resolved.candidates else "onchain_candidates_unavailable"
        payload = OnchainTelegramEditPayload(
            message_id=update.message_id,
            text=_render_session(resolved),
            keyboard=_session_keyboard(resolved, execution_available=self._can_execute(resolved)),
        )
        stored = await self._repository.set_candidates_and_begin_edit(
            resolving.session_id,
            candidates=result.candidates[:6],
            provider_errors=result.provider_errors,
            update_id=update.update_id,
            payload=payload,
            result_code=result_code,
            now_ms=resolved.updated_at_ms,
        )
        if stored is None:
            raise RuntimeError("onchain_candidate_settlement_conflict")
        _, effect = stored
        return await self._deliver_edit_effect(update, effect)

    async def _resolve(self, session: OnchainAnalysisSession, ticker: str) -> OnchainAnalysisSession:
        result, projected = await self._resolved_projection(session, ticker)
        stored = await self._repository.set_candidates(
            session.session_id,
            candidates=result.candidates[:6],
            provider_errors=result.provider_errors,
            now_ms=projected.updated_at_ms,
        )
        if stored is None:
            raise RuntimeError("onchain_candidate_settlement_conflict")
        return stored

    async def _resolved_projection(
        self,
        session: OnchainAnalysisSession,
        ticker: str,
    ) -> tuple[OnchainCandidateResult, OnchainAnalysisSession]:
        result = await self._gateway.resolve(ticker)
        candidates = result.candidates[:6]
        now_ms = int(self._clock_ms())
        return result, session.model_copy(
            update={
                "state": (OnchainAnalysisState.AWAITING_CONTRACT if candidates else OnchainAnalysisState.UNAVAILABLE),
                "candidates": candidates,
                "selected_candidate": None,
                "analysis": None,
                "provider_errors": result.provider_errors,
                "updated_at_ms": now_ms,
            }
        )

    async def _select_candidate(
        self,
        update: TelegramTradingUpdate,
        session: OnchainAnalysisSession,
        candidate_index: int,
    ) -> str:
        quoting = await self._repository.begin_quote(
            session.session_id,
            candidate_index=candidate_index,
            now_ms=int(self._clock_ms()),
        )
        if quoting is None or quoting.selected_candidate is None:
            await self._bot.answer(update.callback_query_id, text="链上合约候选无效或已过期。", show_alert=True)
            return "onchain_candidate_invalid"
        return await self._collect_and_render(update, quoting)

    async def _refresh(self, update: TelegramTradingUpdate, session: OnchainAnalysisSession) -> str:
        quoting = await self._repository.begin_quote(
            session.session_id,
            candidate_index=None,
            now_ms=int(self._clock_ms()),
        )
        if quoting is None or quoting.selected_candidate is None:
            await self._bot.answer(update.callback_query_id, text="当前没有可刷新的链上合约。", show_alert=True)
            return "onchain_refresh_invalid"
        return await self._collect_and_render(update, quoting)

    async def _collect_and_render(
        self,
        update: TelegramTradingUpdate,
        session: OnchainAnalysisSession,
    ) -> str:
        if session.selected_candidate is None:
            raise RuntimeError("onchain_selected_candidate_invariant")
        result = await self._gateway.quote(session.selected_candidate)
        now_ms = int(self._clock_ms())
        projected = session.model_copy(
            update={
                "state": (
                    OnchainAnalysisState.ANALYZED
                    if result.analysis.winner_provider is not None
                    else OnchainAnalysisState.UNAVAILABLE
                ),
                "analysis": result.analysis,
                "provider_errors": result.provider_errors,
                "updated_at_ms": now_ms,
            }
        )
        result_code = (
            "onchain_analysis_ready" if result.analysis.winner_provider is not None else "onchain_routes_unavailable"
        )
        payload = OnchainTelegramEditPayload(
            message_id=update.message_id,
            text=_render_analysis(projected, result),
            keyboard=_session_keyboard(projected, execution_available=self._can_execute(projected)),
        )
        stored = await self._repository.set_analysis_and_begin_edit(
            session.session_id,
            result=result,
            update_id=update.update_id,
            payload=payload,
            result_code=result_code,
            now_ms=now_ms,
        )
        if stored is None:
            raise RuntimeError("onchain_analysis_settlement_conflict")
        _, effect = stored
        return await self._deliver_edit_effect(update, effect)

    async def _cancel(self, update: TelegramTradingUpdate, session: OnchainAnalysisSession) -> str:
        execution = await self._repository.execution_for_session(session.session_id)
        if execution is not None and execution.state is OnchainExecutionState.AWAITING_CONFIRMATION:
            await self._repository.cancel_execution(
                session.session_id,
                update_id=update.update_id,
                now_ms=int(self._clock_ms()),
            )
            return await self._store_and_deliver_edit(
                update,
                session,
                text="已取消本次链上交易；路由分析仍保留，可重新刷新报价。",
                keyboard=(("刷新报价", f"tf:o:r:{session.session_id}"),),
                result_code="onchain_execution_cancelled",
            )
        payload = OnchainTelegramEditPayload(
            message_id=update.message_id,
            text="已取消本次链上路由分析。",
            keyboard=(),
        )
        effect = await self._repository.cancel_and_begin_edit(
            session.session_id,
            update_id=update.update_id,
            payload=payload,
            result_code="onchain_cancelled",
            now_ms=int(self._clock_ms()),
        )
        if effect is None:
            await self._bot.answer(update.callback_query_id, text="本次链上分析已经取消。")
            return "onchain_already_cancelled"
        return await self._deliver_edit_effect(update, effect)

    async def _prepare_execution(
        self,
        update: TelegramTradingUpdate,
        session: OnchainAnalysisSession,
    ) -> str:
        if not self._execution_available or self._wallet_address is None:
            await self._bot.answer(
                update.callback_query_id,
                text="统一链上钱包尚未完成配置，当前只能分析路由。",
                show_alert=True,
            )
            return "onchain_execution_unavailable"
        analysis = session.analysis
        candidate = session.selected_candidate
        if analysis is None or candidate is None or analysis.winner_provider is None:
            await self._bot.answer(update.callback_query_id, text="请先取得可用的链上报价。", show_alert=True)
            return "onchain_execution_quote_missing"
        if analysis.winner_provider not in self._executable_providers:
            await self._bot.answer(
                update.callback_query_id,
                text="当前最佳路由只支持分析，尚未通过可签名交易数据校验。请刷新报价后再试。",
                show_alert=True,
            )
            return "onchain_execution_provider_unavailable"
        if not self._executor_live:
            await self._bot.answer(
                update.callback_query_id,
                text="统一链上钱包执行器暂不可用，请稍后刷新报价。",
                show_alert=True,
            )
            return "onchain_executor_unavailable"
        if candidate.chain_id not in self._execution_assets:
            await self._bot.answer(update.callback_query_id, text="该网络尚未配置执行 RPC。", show_alert=True)
            return "onchain_execution_rpc_missing"
        quote = next(
            (value for value in analysis.eligible_quotes if value.provider == analysis.winner_provider),
            None,
        )
        if quote is None:
            raise RuntimeError("onchain_execution_winner_quote_invariant")
        request = OnchainQuoteRequest(
            chain_id=quote.chain_id,
            input_contract=quote.input_contract,
            output_contract=quote.output_contract,
            input_amount_raw=quote.input_amount_raw,
            slippage_bps=quote.slippage_bps,
        )
        execution, _ = await self._repository.begin_execution(
            execution_id=str(uuid4()),
            session=session,
            provider=analysis.winner_provider,
            wallet_address=self._wallet_address,
            request=request,
            quote=quote,
            now_ms=int(self._clock_ms()),
        )
        return await self._store_and_deliver_edit(
            update,
            session,
            text=_render_execution_confirmation(execution, candidate, self._execution_assets[candidate.chain_id]),
            keyboard=(
                ("确认交易", f"tf:o:y:{session.session_id}"),
                ("取消", f"tf:o:x:{session.session_id}"),
            ),
            result_code="onchain_execution_confirmation_required",
        )

    async def _confirm_execution(
        self,
        update: TelegramTradingUpdate,
        session: OnchainAnalysisSession,
    ) -> str:
        execution = await self._repository.execution_for_session(session.session_id)
        if execution is None:
            await self._bot.answer(update.callback_query_id, text="没有待确认的链上交易。", show_alert=True)
            return "onchain_execution_missing"
        if execution.state is OnchainExecutionState.AWAITING_CONFIRMATION:
            if execution.provider not in self._executable_providers or not self._executor_live:
                await self._bot.answer(
                    update.callback_query_id,
                    text="统一链上钱包执行器暂不可用，本次交易尚未提交。",
                    show_alert=True,
                )
                return "onchain_executor_unavailable"
            if not await self._repository.confirm_execution(
                session.session_id,
                update_id=update.update_id,
                now_ms=int(self._clock_ms()),
            ):
                raise RuntimeError("onchain_execution_confirmation_conflict")
            execution = await self._repository.execution_for_session(session.session_id)
            if execution is None:
                raise RuntimeError("onchain_execution_confirmation_missing")
        return await self._store_and_deliver_edit(
            update,
            session,
            text=_render_execution_status(execution),
            keyboard=(("查看交易状态", f"tf:o:s:{session.session_id}"),),
            result_code="onchain_execution_queued",
        )

    async def _execution_status(
        self,
        update: TelegramTradingUpdate,
        session: OnchainAnalysisSession,
    ) -> str:
        execution = await self._repository.execution_for_session(session.session_id)
        if execution is None:
            await self._bot.answer(update.callback_query_id, text="没有链上交易记录。", show_alert=True)
            return "onchain_execution_missing"
        terminal = execution.state in {
            OnchainExecutionState.CONFIRMED,
            OnchainExecutionState.FAILED,
            OnchainExecutionState.AMBIGUOUS,
            OnchainExecutionState.CANCELLED,
        }
        return await self._store_and_deliver_edit(
            update,
            session,
            text=_render_execution_status(execution),
            keyboard=() if terminal else (("刷新交易状态", f"tf:o:s:{session.session_id}"),),
            result_code="onchain_execution_status",
        )

    async def _store_and_deliver_edit(
        self,
        update: TelegramTradingUpdate,
        session: OnchainAnalysisSession,
        *,
        text: str,
        keyboard: tuple[tuple[str, str], ...],
        result_code: str,
    ) -> str:
        effect = await self._repository.begin_edit(
            session.session_id,
            update_id=update.update_id,
            payload=OnchainTelegramEditPayload(
                message_id=update.message_id,
                text=text,
                keyboard=keyboard,
            ),
            result_code=result_code,
            now_ms=int(self._clock_ms()),
        )
        return await self._deliver_edit_effect(update, effect)

    def _can_execute(self, session: OnchainAnalysisSession) -> bool:
        candidate = session.selected_candidate
        winner = session.analysis.winner_provider if session.analysis is not None else None
        return bool(
            self._execution_available
            and self._executor_live
            and self._wallet_address is not None
            and winner in self._executable_providers
            and candidate is not None
            and candidate.chain_id in self._execution_assets
        )

    async def _refresh_executor_live(self) -> None:
        self._executor_live = False
        if not self._execution_available or self._wallet_address is None:
            return
        self._executor_live = await self._repository.executor_available(
            wallet_fingerprint=onchain_wallet_fingerprint(self._wallet_address),
            now_ms=int(self._clock_ms()),
        )

    async def _deliver_edit_effect(
        self,
        update: TelegramTradingUpdate,
        effect: OnchainTelegramEditEffect,
    ) -> str:
        if effect.state is OnchainTelegramEditState.SENT:
            await self._bot.answer(update.callback_query_id, text=_edit_effect_answer(effect.result_code))
            return effect.result_code
        if effect.state is OnchainTelegramEditState.AMBIGUOUS:
            await self._bot.answer(
                update.callback_query_id,
                text="消息更新结果不确定；已保留数据库事实并停止重复编辑。",
                show_alert=True,
            )
            return "onchain_edit_ambiguous"
        try:
            await self._bot.edit(
                message_id=effect.payload.message_id,
                text=effect.payload.text,
                keyboard=effect.payload.keyboard,
            )
        except Exception:
            marked = await self._repository.settle_edit_ambiguous(
                effect.session_id,
                update_id=effect.update_id,
                error_code="telegram_edit_result_ambiguous",
                now_ms=int(self._clock_ms()),
            )
            if not marked:
                raise RuntimeError("onchain_edit_ambiguity_conflict") from None
            with suppress(Exception):
                await self._bot.answer(
                    update.callback_query_id,
                    text="消息更新结果不确定；数据库事实已保留，本次不会盲目重发。",
                    show_alert=True,
                )
            return "onchain_edit_ambiguous"
        if not await self._repository.settle_edit_sent(
            effect.session_id,
            update_id=effect.update_id,
            now_ms=int(self._clock_ms()),
        ):
            raise RuntimeError("onchain_edit_settlement_conflict")
        await self._bot.answer(update.callback_query_id, text=_edit_effect_answer(effect.result_code))
        return effect.result_code


def _parse_action(data: str) -> tuple[str, str | None, str] | None:
    parts = data.split(":")
    try:
        if len(parts) == 5 and parts[:3] == ["tf", "o", "a"]:
            if re.fullmatch(r"[0-3]", parts[3]) is None:
                return None
            return "asset", parts[3], _canonical_session_id(parts[4])
        if len(parts) == 5 and parts[:3] == ["tf", "o", "c"]:
            if re.fullmatch(r"[0-5]", parts[3]) is None:
                return None
            return "candidate", parts[3], _canonical_session_id(parts[4])
        if len(parts) == 4 and parts[:3] in (["tf", "o", "r"], ["tf", "o", "x"]):
            return ("refresh" if parts[2] == "r" else "cancel"), None, _canonical_session_id(parts[3])
        if len(parts) == 4 and parts[:3] in (
            ["tf", "o", "p"],
            ["tf", "o", "y"],
            ["tf", "o", "s"],
        ):
            kind = {"p": "prepare", "y": "confirm", "s": "status"}[parts[2]]
            return kind, None, _canonical_session_id(parts[3])
    except ValueError:
        return None
    return None


def _canonical_session_id(value: str) -> str:
    parsed = UUID(str(value))
    canonical = str(parsed)
    if canonical != value:
        raise ValueError("onchain_session_id_noncanonical")
    return canonical


def _session_bound(session: OnchainAnalysisSession | None, update: TelegramTradingUpdate) -> bool:
    return bool(
        session is not None
        and session.actor_user_id == update.actor_user_id
        and session.chat_id == update.chat_id
        and session.interaction_message_id == update.message_id
    )


def _edit_effect_answer(result_code: str) -> str:
    return {
        "onchain_candidates_ready": "已按 TG 展示标的查找链上合约。",
        "onchain_candidates_unavailable": "未找到可用的链上合约候选。",
        "onchain_analysis_ready": "链上报价已更新。",
        "onchain_routes_unavailable": "当前没有可用的链上报价。",
        "onchain_cancelled": "已取消。",
    }.get(result_code, "链上分析已更新。")


def _session_keyboard(
    session: OnchainAnalysisSession,
    *,
    execution_available: bool = False,
) -> tuple[tuple[str, str], ...]:
    if session.state is OnchainAnalysisState.AWAITING_TICKER:
        return (
            *((source.ticker, f"tf:o:a:{index}:{session.session_id}") for index, source in enumerate(session.sources)),
            ("取消", f"tf:o:x:{session.session_id}"),
        )
    if session.state is OnchainAnalysisState.AWAITING_CONTRACT:
        buttons = tuple(
            (
                f"{_compact_chain_name(candidate.chain_name)} · {candidate.symbol} · "
                f"{_short_contract(candidate.contract_address)}",
                f"tf:o:c:{index}:{session.session_id}",
            )
            for index, candidate in enumerate(session.candidates)
        )
        return (*buttons, ("取消", f"tf:o:x:{session.session_id}"))
    if session.state is OnchainAnalysisState.ANALYZED:
        trading = (("使用统一钱包交易", f"tf:o:p:{session.session_id}"),) if execution_available else ()
        return (*trading, ("刷新报价", f"tf:o:r:{session.session_id}"), ("取消", f"tf:o:x:{session.session_id}"))
    return (("取消", f"tf:o:x:{session.session_id}"),)


def _render_session(session: OnchainAnalysisSession) -> str:
    if session.state is OnchainAnalysisState.AWAITING_TICKER:
        return "🎯 <b>选择链上标的</b>\n\n这条 TG 新闻展示了多个标的，请先选择要分析的标的。"
    if session.candidates:
        lines = [
            f"🔎 <b>选择链上合约 · {html.escape(session.selected_ticker or '')}</b>",
            "",
            "同名代币可能跨链，请按网络和短 CA 选择。",
        ]
        for index, candidate in enumerate(session.candidates, start=1):
            verified = "已验证" if candidate.verified else "未验证"
            providers = "/".join(_provider_label(value) for value in candidate.providers)
            liquidity = "" if candidate.liquidity_usd is None else f" · 流动性 ${_compact_usd(candidate.liquidity_usd)}"
            pool_count = f" · {candidate.pair_count} 池" if candidate.pair_count else ""
            lines.append(
                f"{index}. <b>{html.escape(_compact_chain_name(candidate.chain_name))} · "
                f"{html.escape(candidate.name)}</b>  <code>{_short_contract(candidate.contract_address)}</code>\n"
                f"   {verified} · {Decimal(candidate.confidence_bps) / 100:.0f}% · {providers}"
                f"{liquidity}{pool_count}"
            )
        lines.extend(["", "点击下方合约继续报价。"])
        lines.extend(_provider_error_lines(session.provider_errors))
        return "\n".join(lines)
    return (
        f"⚠️ <b>未找到可信链上合约：{html.escape(session.selected_ticker or '')}</b>\n\n"
        "系统只查询 TG 卡片展示的 ticker/CA，没有从新闻正文猜测其他标的。"
        + "\n".join(_provider_error_lines(session.provider_errors))
    )


def _render_analysis(session: OnchainAnalysisSession, result: OnchainQuoteResult) -> str:
    analysis = result.analysis
    candidate = session.selected_candidate
    if candidate is None:
        raise RuntimeError("onchain_analysis_candidate_invariant")
    lines = [
        f"🧭 <b>链上最佳路由：{html.escape(candidate.symbol)}</b>",
        f"网络：{html.escape(candidate.chain_name)}（{candidate.chain_id}）",
        f"CA：<code>{candidate.contract_address}</code>",
        "",
    ]
    if analysis.winner_provider is None:
        lines.append("❌ 当前没有通过硬性检查的可用路由。")
    else:
        prefix = "确定最佳" if analysis.state is RouteAnalysisState.DEFINITIVE else "暂定最佳"
        lines.append(f"{prefix}：<b>{_provider_label(analysis.winner_provider)}</b>")
        if analysis.state is RouteAnalysisState.PROVISIONAL:
            lines.append("⚠️ 成本或安全数据不完整，只按同额输入的预计到账量暂定排序。")
            lines.extend(_analysis_reason_lines(analysis.reason_codes))
        lines.append("")
        for quote in analysis.eligible_quotes:
            output = Decimal(quote.expected_output_raw) / (Decimal(10) ** result.output_decimals)
            minimum = (
                "未知"
                if quote.minimum_output_raw is None
                else (
                    f"{Decimal(quote.minimum_output_raw) / (Decimal(10) ** result.output_decimals):.8f} "
                    f"{html.escape(candidate.symbol)}"
                )
            )
            if quote.gas_fee_usd is not None:
                gas = f"gas 美元成本 ${quote.gas_fee_usd}"
            elif quote.gas_limit is not None:
                gas = f"gas 用量估算 {quote.gas_limit}，美元成本未知"
            else:
                gas = "gas 美元成本未知"
            route = " → ".join(quote.route_labels) if quote.route_labels else "路由明细未返回"
            lines.extend(
                [
                    (
                        f"• <b>{_provider_label(quote.provider)}</b>：预计到账 "
                        f"{output:.8f} {html.escape(candidate.symbol)}"
                    ),
                    f"  最低到账 {minimum} · {gas}",
                    f"  {html.escape(route)} · {quote.latency_ms}ms",
                ]
            )
    lines.extend(_provider_error_lines(result.provider_errors))
    lines.extend(["", "交易时所有可执行路由共用同一个手动链上钱包；Provider Key 不具备资金权限。"])
    return "\n".join(lines)


def _render_execution_confirmation(
    execution: OnchainExecutionIntent,
    candidate: OnchainAssetCandidate,
    settlement: tuple[str, int],
) -> str:
    settlement_symbol, settlement_decimals = settlement
    spend = Decimal(execution.request.input_amount_raw) / (Decimal(10) ** settlement_decimals)
    expected = Decimal(execution.quote.expected_output_raw) / (Decimal(10) ** candidate.decimals)
    minimum = (
        "未知"
        if execution.quote.minimum_output_raw is None
        else f"{Decimal(execution.quote.minimum_output_raw) / (Decimal(10) ** candidate.decimals):.8f}"
    )
    warning = (
        "\n⚠️ 当前路由证据仍是暂定排序；确认后执行器会重新询价，低于本次最低到账量会拒绝。"
        if not execution.quote.complete_for_definitive_ranking
        else ""
    )
    return (
        f"🔐 <b>确认链上交易</b>\n\n"
        f"路由：<b>{_provider_label(execution.provider)}</b>\n"
        f"钱包：<code>{execution.wallet_address}</code>\n"
        f"支付：{spend} {html.escape(settlement_symbol)}\n"
        f"预计到账：{expected:.8f} {html.escape(candidate.symbol)}\n"
        f"最低到账：{minimum} {html.escape(candidate.symbol)}\n"
        f"网络：{html.escape(candidate.chain_name)}\n"
        f"CA：<code>{candidate.contract_address}</code>{warning}\n\n"
        "点击确认后，独立执行进程才会使用同一个钱包私钥签名；可能先产生一笔精确额度授权交易。"
    )


def _render_execution_status(execution: OnchainExecutionIntent) -> str:
    labels = {
        OnchainExecutionState.AWAITING_CONFIRMATION: "等待确认",
        OnchainExecutionState.PENDING: "已排队",
        OnchainExecutionState.CLAIMED: "正在重新询价并构造交易",
        OnchainExecutionState.APPROVAL_SUBMITTED: "授权交易已提交",
        OnchainExecutionState.SWAP_SUBMITTED: "兑换交易已提交",
        OnchainExecutionState.CONFIRMED: "交易已确认",
        OnchainExecutionState.FAILED: "交易失败",
        OnchainExecutionState.AMBIGUOUS: "链上结果待人工核对",
        OnchainExecutionState.CANCELLED: "交易已取消",
    }
    lines = [
        "⛓️ <b>链上交易状态</b>",
        "",
        f"状态：<b>{labels[execution.state]}</b>",
        f"路由：{_provider_label(execution.provider)}",
        f"钱包：<code>{execution.wallet_address}</code>",
    ]
    if execution.approval_transaction is not None:
        lines.append(f"授权 Tx：<code>{execution.approval_transaction.transaction_hash}</code>")
    if execution.swap_transaction is not None:
        lines.append(f"兑换 Tx：<code>{execution.swap_transaction.transaction_hash}</code>")
    if execution.error_code is not None:
        lines.append(f"错误：<code>{html.escape(execution.error_code)}</code>")
    return "\n".join(lines)


def _analysis_reason_lines(reason_codes: tuple[str, ...]) -> list[str]:
    labels = {
        "cost_incomplete": "费用折算",
        "simulation_incomplete": "交易模拟",
        "risk_check_incomplete": "代币风控",
    }
    missing = [labels[code] for code in reason_codes if code in labels]
    return [f"缺少证据：{'、'.join(missing)}"] if missing else []


def _provider_error_lines(errors: tuple[str, ...]) -> list[str]:
    lines: list[str] = []
    for code in errors:
        label = {
            "binance_general_web3_swap_api_unpublished": "币安：通用 Web3 Swap 官方接口尚未公开",
            "okx_request_failed": "OKX：报价服务暂不可用",
            "okx_provider_rejected": "OKX：报价请求被拒绝",
            "oneinch_request_failed": "1inch：报价服务暂不可用",
            "dexscreener_token_search_request_failed": "长尾代币发现：市场目录暂不可用",
            "dexscreener_onchain_metadata_unavailable": "长尾代币发现：链上合约元数据暂不可用",
        }.get(code, "路由提供方：暂不可用")
        lines.append(f"\nℹ️ {label}")
    return lines


def _provider_label(provider: str) -> str:
    return {
        "okx": "OKX",
        "oneinch": "1inch",
        "binance": "币安",
        "dexscreener": "DEX Screener + 链上元数据",
    }.get(provider, provider)


def _short_contract(value: str) -> str:
    return f"{value[:6]}…{value[-4:]}"


def _compact_chain_name(value: str) -> str:
    return {
        "Robinhood Chain": "Robinhood",
        "BNB Chain": "BNB",
        "Arbitrum One": "Arbitrum",
    }.get(value, value)


def _compact_usd(value: Decimal) -> str:
    if value >= Decimal("1000000"):
        return f"{value / Decimal('1000000'):.2f}M"
    if value >= Decimal("1000"):
        return f"{value / Decimal('1000'):.0f}K"
    return f"{value:,.0f}"


__all__ = [
    "OnchainCandidateResult",
    "OnchainQuoteResult",
    "OnchainTelegramTradingController",
]
