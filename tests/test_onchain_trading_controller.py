from __future__ import annotations

import asyncio
from dataclasses import replace
from decimal import Decimal
from typing import Any

from tracefold.app.onchain_trading import (
    OnchainCandidateResult,
    OnchainQuoteResult,
    OnchainTelegramTradingController,
)
from tracefold.integrations.telegram import TelegramTradingUpdate
from tracefold.trading.onchain import (
    OnchainAnalysisSession,
    OnchainAnalysisState,
    OnchainAssetCandidate,
    OnchainInteractionReplyState,
    OnchainNewsSource,
    OnchainRouteQuote,
    OnchainTelegramEditEffect,
    OnchainTelegramEditPayload,
    OnchainTelegramEditState,
    analyze_onchain_routes,
)

NOW = 1_900_000_000_000
SESSION_ID = "0198f3ae-76c0-77a1-a191-0d3f16842ea0"
TARGET = "a" * 64
CHANNEL_ID = -1001234567890
OPERATOR_ID = 123456789
USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
HYPE_ETH = "0x1111111111111111111111111111111111111111"
HYPE_BASE = "0x2222222222222222222222222222222222222222"


def _source(ticker: str) -> OnchainNewsSource:
    return OnchainNewsSource(
        news_event_id="event-42",
        delivery_target_sha256=TARGET,
        delivery_message_id=42,
        headline_zh="新闻正文还提到了 BTC，但展示标的是 HYPE",
        ticker=ticker,
        source_observed_at_ms=NOW - 1_000,
    )


def _candidate(chain_id: int, address: str, chain_name: str) -> OnchainAssetCandidate:
    return OnchainAssetCandidate(
        chain_id=chain_id,
        chain_name=chain_name,
        contract_address=address,
        symbol="HYPE",
        name="Hyperliquid",
        decimals=18,
        providers=("okx", "oneinch"),
        verified=True,
        confidence_bps=9_000,
    )


class _Repository:
    def __init__(self, sources: tuple[OnchainNewsSource, ...]) -> None:
        self.sources = sources
        self.session: OnchainAnalysisSession | None = None
        self.effects: dict[int, OnchainTelegramEditEffect] = {}
        self.fail_next_edit_settlement = False
        self.executor_live = True
        self.executor_checks: list[tuple[str, int]] = []

    async def sources_for_message(self, message_id: int) -> tuple[OnchainNewsSource, ...]:
        return self.sources if message_id == 42 else ()

    async def begin_session(self, **values: Any) -> tuple[OnchainAnalysisSession, bool]:
        if self.session is not None:
            return self.session, False
        selected = values["selected_ticker"]
        self.session = OnchainAnalysisSession(
            session_id=values["session_id"],
            sources=values["sources"],
            actor_user_id=values["actor_user_id"],
            chat_id=values["chat_id"],
            source_message_id=values["sources"][0].delivery_message_id,
            state=OnchainAnalysisState.RESOLVING if selected else OnchainAnalysisState.AWAITING_TICKER,
            selected_ticker=selected,
            created_at_ms=values["now_ms"],
            updated_at_ms=values["now_ms"],
        )
        return self.session, True

    async def begin_interaction_reply(self, session_id: str, *, now_ms: int) -> bool:
        assert self.session is not None and self.session.session_id == session_id
        if self.session.interaction_reply_state is not OnchainInteractionReplyState.PENDING:
            return False
        self.session = self.session.model_copy(
            update={
                "interaction_reply_attempted_at_ms": now_ms,
                "interaction_reply_state": OnchainInteractionReplyState.SENDING,
            }
        )
        return True

    async def attach_interaction_message(self, session_id: str, *, message_id: int, now_ms: int) -> bool:
        assert self.session is not None and self.session.session_id == session_id
        self.session = self.session.model_copy(
            update={
                "interaction_message_id": message_id,
                "interaction_reply_state": OnchainInteractionReplyState.SENT,
                "updated_at_ms": now_ms,
            }
        )
        return True

    async def mark_interaction_reply_ambiguous(
        self,
        session_id: str,
        *,
        error_code: str,
        now_ms: int,
    ) -> bool:
        assert self.session is not None and self.session.session_id == session_id
        if self.session.interaction_reply_state is not OnchainInteractionReplyState.SENDING:
            return False
        self.session = self.session.model_copy(
            update={
                "interaction_reply_state": OnchainInteractionReplyState.AMBIGUOUS,
                "interaction_reply_error_code": error_code,
                "updated_at_ms": now_ms,
            }
        )
        return True

    async def get_session(self, session_id: str) -> OnchainAnalysisSession | None:
        return self.session if self.session is not None and self.session.session_id == session_id else None

    async def executor_available(self, *, wallet_fingerprint: str, now_ms: int) -> bool:
        self.executor_checks.append((wallet_fingerprint, now_ms))
        return self.executor_live

    async def begin_resolution(self, session_id: str, *, ticker: str, now_ms: int) -> OnchainAnalysisSession | None:
        assert self.session is not None and self.session.session_id == session_id
        if ticker not in {source.ticker for source in self.session.sources}:
            return None
        self.session = self.session.model_copy(
            update={
                "state": OnchainAnalysisState.RESOLVING,
                "selected_ticker": ticker,
                "candidates": (),
                "selected_candidate": None,
                "analysis": None,
                "provider_errors": (),
                "updated_at_ms": now_ms,
            }
        )
        return self.session

    async def set_candidates(
        self,
        session_id: str,
        *,
        candidates: tuple[OnchainAssetCandidate, ...],
        provider_errors: tuple[str, ...],
        now_ms: int,
    ) -> OnchainAnalysisSession | None:
        assert self.session is not None and self.session.session_id == session_id
        self.session = self.session.model_copy(
            update={
                "state": OnchainAnalysisState.AWAITING_CONTRACT if candidates else OnchainAnalysisState.UNAVAILABLE,
                "candidates": candidates,
                "provider_errors": provider_errors,
                "updated_at_ms": now_ms,
            }
        )
        return self.session

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
    ) -> tuple[OnchainAnalysisSession, OnchainTelegramEditEffect] | None:
        session = await self.set_candidates(
            session_id,
            candidates=candidates,
            provider_errors=provider_errors,
            now_ms=now_ms,
        )
        if session is None:
            return None
        effect = OnchainTelegramEditEffect(
            session_id=session_id,
            update_id=update_id,
            payload=payload,
            result_code=result_code,
            state=OnchainTelegramEditState.SENDING,
            attempted_at_ms=now_ms,
        )
        self.effects[update_id] = effect
        return session, effect

    async def begin_quote(
        self,
        session_id: str,
        *,
        candidate_index: int | None,
        now_ms: int,
    ) -> OnchainAnalysisSession | None:
        assert self.session is not None and self.session.session_id == session_id
        if candidate_index is None:
            candidate = self.session.selected_candidate
        elif 0 <= candidate_index < len(self.session.candidates):
            candidate = self.session.candidates[candidate_index]
        else:
            return None
        if candidate is None:
            return None
        self.session = self.session.model_copy(
            update={
                "state": OnchainAnalysisState.QUOTING,
                "selected_candidate": candidate,
                "analysis": None,
                "updated_at_ms": now_ms,
            }
        )
        return self.session

    async def set_analysis(
        self,
        session_id: str,
        *,
        result: OnchainQuoteResult,
        now_ms: int,
    ) -> OnchainAnalysisSession | None:
        assert self.session is not None and self.session.session_id == session_id
        self.session = self.session.model_copy(
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
        return self.session

    async def set_analysis_and_begin_edit(
        self,
        session_id: str,
        *,
        result: OnchainQuoteResult,
        update_id: int,
        payload: OnchainTelegramEditPayload,
        result_code: str,
        now_ms: int,
    ) -> tuple[OnchainAnalysisSession, OnchainTelegramEditEffect] | None:
        session = await self.set_analysis(session_id, result=result, now_ms=now_ms)
        if session is None:
            return None
        effect = OnchainTelegramEditEffect(
            session_id=session_id,
            update_id=update_id,
            payload=payload,
            result_code=result_code,
            state=OnchainTelegramEditState.SENDING,
            attempted_at_ms=now_ms,
        )
        self.effects[update_id] = effect
        return session, effect

    async def cancel(self, session_id: str, *, now_ms: int) -> bool:
        assert self.session is not None and self.session.session_id == session_id
        self.session = self.session.model_copy(
            update={"state": OnchainAnalysisState.CANCELLED, "updated_at_ms": now_ms}
        )
        return True

    async def cancel_and_begin_edit(
        self,
        session_id: str,
        *,
        update_id: int,
        payload: OnchainTelegramEditPayload,
        result_code: str,
        now_ms: int,
    ) -> OnchainTelegramEditEffect | None:
        if not await self.cancel(session_id, now_ms=now_ms):
            return None
        effect = OnchainTelegramEditEffect(
            session_id=session_id,
            update_id=update_id,
            payload=payload,
            result_code=result_code,
            state=OnchainTelegramEditState.SENDING,
            attempted_at_ms=now_ms,
        )
        self.effects[update_id] = effect
        return effect

    async def edit_effect(
        self,
        session_id: str,
        *,
        update_id: int,
    ) -> OnchainTelegramEditEffect | None:
        effect = self.effects.get(update_id)
        return effect if effect is not None and effect.session_id == session_id else None

    async def settle_edit_sent(self, session_id: str, *, update_id: int, now_ms: int) -> bool:
        effect = self.effects.get(update_id)
        assert effect is not None and effect.session_id == session_id
        if self.fail_next_edit_settlement:
            self.fail_next_edit_settlement = False
            return False
        self.effects[update_id] = effect.model_copy(
            update={"state": OnchainTelegramEditState.SENT, "settled_at_ms": now_ms}
        )
        return True

    async def settle_edit_ambiguous(
        self,
        session_id: str,
        *,
        update_id: int,
        error_code: str,
        now_ms: int,
    ) -> bool:
        effect = self.effects.get(update_id)
        assert effect is not None and effect.session_id == session_id
        self.effects[update_id] = effect.model_copy(
            update={
                "state": OnchainTelegramEditState.AMBIGUOUS,
                "error_code": error_code,
                "settled_at_ms": now_ms,
            }
        )
        return True


class _Gateway:
    def __init__(self, *, quote_provider: str = "okx") -> None:
        self.resolved_tickers: list[str] = []
        self.quoted: list[OnchainAssetCandidate] = []
        self.candidates = (
            _candidate(1, HYPE_ETH, "Ethereum"),
            _candidate(8453, HYPE_BASE, "Base"),
        )
        self.quote_provider = quote_provider

    async def close(self) -> None:
        return None

    async def resolve(self, ticker: str) -> OnchainCandidateResult:
        self.resolved_tickers.append(ticker)
        return OnchainCandidateResult(
            candidates=self.candidates,
            provider_errors=("binance_general_web3_swap_api_unpublished",),
        )

    async def quote(self, candidate: OnchainAssetCandidate) -> OnchainQuoteResult:
        self.quoted.append(candidate)
        quote = OnchainRouteQuote(
            provider=self.quote_provider,
            chain_id=candidate.chain_id,
            input_contract=USDC,
            output_contract=candidate.contract_address,
            input_amount_raw=10_000_000,
            expected_output_raw=1_000_000_000_000_000_000,
            minimum_output_raw=None,
            expected_output_usd=None,
            provider_fee_usd=None,
            gas_fee_usd=None,
            slippage_bps=100,
            route_labels=("Uniswap V3",),
            latency_ms=200,
            received_at_ms=NOW,
            expires_at_ms=NOW + 15_000,
            simulation_passed=None,
            risk_checked=False,
        )
        return OnchainQuoteResult(
            analysis=analyze_onchain_routes((quote,), now_ms=NOW),
            settlement_symbol="USDC",
            settlement_decimals=6,
            output_decimals=18,
            provider_errors=("binance_general_web3_swap_api_unpublished",),
        )


class _Bot:
    def __init__(self) -> None:
        self.answers: list[tuple[str, bool]] = []
        self.replies: list[dict[str, object]] = []
        self.edits: list[dict[str, object]] = []

    async def answer(self, _callback_query_id: str, *, text: str, show_alert: bool = False) -> None:
        self.answers.append((text, show_alert))

    async def reply(self, **values: Any) -> int:
        self.replies.append(values)
        return 99

    async def edit(self, **values: Any) -> None:
        self.edits.append(values)


class _FailFirstReplyBot(_Bot):
    def __init__(self) -> None:
        super().__init__()
        self.failed = False

    async def reply(self, **values: Any) -> int:
        if not self.failed:
            self.failed = True
            raise RuntimeError("telegram_temporarily_unavailable")
        return await super().reply(**values)


class _FailFirstEditBot(_Bot):
    def __init__(self) -> None:
        super().__init__()
        self.failed = False

    async def edit(self, **values: Any) -> None:
        if not self.failed:
            self.failed = True
            raise RuntimeError("telegram_edit_temporarily_unavailable")
        await super().edit(**values)


def _update(
    data: str,
    *,
    message_id: int = 42,
    actor_user_id: int = OPERATOR_ID,
    chat_id: int = CHANNEL_ID,
) -> TelegramTradingUpdate:
    return TelegramTradingUpdate(
        update_id=101,
        callback_query_id="callback-1",
        actor_user_id=actor_user_id,
        chat_id=chat_id,
        message_id=message_id,
        data=data,
        authorized=True,
    )


def _controller(
    sources: tuple[OnchainNewsSource, ...],
    *,
    execution_available: bool = False,
    execution_notional_usd: Decimal = Decimal("10"),
) -> tuple[Any, _Repository, _Gateway, _Bot]:
    repository = _Repository(sources)
    gateway = _Gateway(quote_provider="oneinch" if execution_available else "okx")
    bot = _Bot()
    controller = OnchainTelegramTradingController(
        repository=repository,
        gateway=gateway,
        bot=bot,
        wallet_address=("0x7e5f4552091a69125d5dfcb7b8c2659029395bdf" if execution_available else None),
        execution_assets={1: ("USDC", 6, execution_notional_usd)} if execution_available else None,
        execution_available=execution_available,
        executable_providers=("oneinch",) if execution_available else (),
        clock_ms=lambda: NOW,
        session_id_factory=lambda: SESSION_ID,
    )
    return controller, repository, gateway, bot


def test_trade_button_requires_live_shared_wallet_executor_heartbeat() -> None:
    controller, repository, _gateway, bot = _controller((_source("HYPE"),), execution_available=True)
    repository.executor_live = False

    asyncio.run(controller.handle(_update("tf:onchain:v1")))
    selected = replace(_update(f"tf:o:c:0:{SESSION_ID}", message_id=99), update_id=102)
    assert asyncio.run(controller.handle(selected)) == "onchain_analysis_ready"

    callbacks = [value for _, value in bot.edits[-1]["keyboard"]]  # type: ignore[union-attr]
    assert f"tf:o:p:{SESSION_ID}" not in callbacks
    crafted = replace(_update(f"tf:o:p:{SESSION_ID}", message_id=99), update_id=103)
    assert asyncio.run(controller.handle(crafted)) == "onchain_executor_unavailable"
    assert repository.executor_checks

    repository.executor_live = True
    refreshed = replace(_update(f"tf:o:r:{SESSION_ID}", message_id=99), update_id=104)
    assert asyncio.run(controller.handle(refreshed)) == "onchain_analysis_ready"
    callbacks = [value for _, value in bot.edits[-1]["keyboard"]]  # type: ignore[union-attr]
    assert f"tf:o:p:{SESSION_ID}" in callbacks


def test_development_test_execution_refuses_configured_notional_above_200u() -> None:
    source = _source("HYPE").model_copy(update={"news_event_id": "development-test:fixture"})
    controller, repository, _gateway, bot = _controller(
        (source,),
        execution_available=True,
        execution_notional_usd=Decimal("200.01"),
    )

    asyncio.run(controller.handle(_update("tf:onchain:v1")))
    selected = replace(_update(f"tf:o:c:0:{SESSION_ID}", message_id=99), update_id=102)
    assert asyncio.run(controller.handle(selected)) == "onchain_analysis_ready"
    prepare = replace(_update(f"tf:o:p:{SESSION_ID}", message_id=99), update_id=103)

    assert asyncio.run(controller.handle(prepare)) == "onchain_development_test_notional_cap"
    assert repository.session is not None
    assert "200U" in bot.answers[-1][0]


def test_single_displayed_ticker_resolves_ca_candidates_then_quotes_selected_identity() -> None:
    controller, repository, gateway, bot = _controller((_source("HYPE"),))

    assert asyncio.run(controller.handle(_update("tf:onchain:v1"))) == "onchain_candidates_ready"

    assert gateway.resolved_tickers == ["HYPE"]
    assert repository.session is not None and repository.session.interaction_message_id == 99
    assert "选择链上合约" in str(bot.replies[-1]["text"])
    assert "Ethereum" in str(bot.replies[-1]["text"])
    rendered_picker = str(bot.replies[-1]["text"])
    assert HYPE_ETH not in rendered_picker
    assert "0x1111…1111" in rendered_picker
    assert "新闻标的只是 ticker/CA 搜索种子" not in rendered_picker
    assert "证据：已验证证据" not in rendered_picker
    assert len(rendered_picker) < 480
    labels = [label for label, _ in bot.replies[-1]["keyboard"]]  # type: ignore[union-attr]
    assert labels[:2] == ["Ethereum · HYPE · 0x1111…1111", "Base · HYPE · 0x2222…2222"]
    callbacks = [value for _, value in bot.replies[-1]["keyboard"]]  # type: ignore[union-attr]
    assert callbacks[:2] == [f"tf:o:c:0:{SESSION_ID}", f"tf:o:c:1:{SESSION_ID}"]
    assert all(len(value.encode()) <= 64 for value in callbacks)

    selected = replace(_update(f"tf:o:c:0:{SESSION_ID}", message_id=99), update_id=102)
    assert asyncio.run(controller.handle(selected)) == "onchain_analysis_ready"

    assert gateway.quoted == [gateway.candidates[0]]
    rendered = str(bot.edits[-1]["text"])
    assert "链上最佳路由" in rendered
    assert "暂定最佳：<b>OKX</b>" in rendered
    assert "成本或安全数据不完整" in rendered
    assert "缺少证据：费用折算、交易模拟、代币风控" in rendered
    assert "最低到账 未知" in rendered
    assert "币安：通用 Web3 Swap 官方接口尚未公开" in rendered


def test_durable_edit_effect_replays_exact_payload_after_settlement_conflict() -> None:
    controller, repository, gateway, bot = _controller((_source("HYPE"),))
    asyncio.run(controller.handle(_update("tf:onchain:v1")))
    selected = replace(_update(f"tf:o:c:0:{SESSION_ID}", message_id=99), update_id=102)
    repository.fail_next_edit_settlement = True

    try:
        asyncio.run(controller.handle(selected))
    except RuntimeError as exc:
        assert str(exc) == "onchain_edit_settlement_conflict"
    else:
        raise AssertionError("expected edit settlement conflict")

    assert repository.session is not None
    assert repository.session.state is OnchainAnalysisState.ANALYZED
    assert repository.effects[102].state is OnchainTelegramEditState.SENDING
    assert asyncio.run(controller.handle(selected)) == "onchain_analysis_ready"
    assert gateway.quoted == [gateway.candidates[0]]
    assert len(bot.edits) == 2 and bot.edits[0] == bot.edits[1]
    assert repository.effects[102].state is OnchainTelegramEditState.SENT


def test_edit_timeout_is_audited_as_ambiguous_without_blind_resend() -> None:
    repository = _Repository((_source("HYPE"),))
    gateway = _Gateway()
    bot = _FailFirstEditBot()
    controller = OnchainTelegramTradingController(
        repository=repository,
        gateway=gateway,
        bot=bot,
        clock_ms=lambda: NOW,
        session_id_factory=lambda: SESSION_ID,
    )
    asyncio.run(controller.handle(_update("tf:onchain:v1")))
    selected = replace(_update(f"tf:o:c:0:{SESSION_ID}", message_id=99), update_id=102)

    assert asyncio.run(controller.handle(selected)) == "onchain_edit_ambiguous"
    assert repository.effects[102].state is OnchainTelegramEditState.AMBIGUOUS
    assert asyncio.run(controller.handle(selected)) == "onchain_edit_ambiguous"
    assert gateway.quoted == [gateway.candidates[0]]
    assert bot.edits == []


def test_interaction_send_failure_is_durable_ambiguous_and_never_blindly_resent() -> None:
    repository = _Repository((_source("HYPE"),))
    gateway = _Gateway()
    bot = _FailFirstReplyBot()
    controller = OnchainTelegramTradingController(
        repository=repository,
        gateway=gateway,
        bot=bot,
        clock_ms=lambda: NOW,
        session_id_factory=lambda: SESSION_ID,
    )

    assert asyncio.run(controller.handle(_update("tf:onchain:v1"))) == "onchain_reply_ambiguous"
    assert gateway.resolved_tickers == ["HYPE"]
    assert repository.session is not None
    assert repository.session.interaction_reply_state is OnchainInteractionReplyState.AMBIGUOUS

    assert asyncio.run(controller.handle(_update("tf:onchain:v1"))) == "onchain_reply_ambiguous"
    assert gateway.resolved_tickers == ["HYPE"]
    assert bot.replies == []


def test_multiple_displayed_tickers_require_ticker_selection_before_ca_resolution() -> None:
    controller, _repository, gateway, bot = _controller((_source("HYPE"), _source("ETH")))

    assert asyncio.run(controller.handle(_update("tf:onchain:v1"))) == "onchain_ticker_selection_required"
    assert gateway.resolved_tickers == []
    assert bot.replies[-1]["keyboard"][:2] == (
        ("HYPE", f"tf:o:a:0:{SESSION_ID}"),
        ("ETH", f"tf:o:a:1:{SESSION_ID}"),
    )

    selected = replace(_update(f"tf:o:a:0:{SESSION_ID}", message_id=99), update_id=102)
    assert asyncio.run(controller.handle(selected)) == "onchain_candidates_ready"
    assert gateway.resolved_tickers == ["HYPE"]


def test_callback_is_bound_to_original_actor_chat_and_interaction_message() -> None:
    controller, _repository, gateway, _bot = _controller((_source("HYPE"),))
    asyncio.run(controller.handle(_update("tf:onchain:v1")))

    mismatches = (
        _update(f"tf:o:c:0:{SESSION_ID}", message_id=99, actor_user_id=777),
        _update(f"tf:o:c:0:{SESSION_ID}", message_id=99, chat_id=CHANNEL_ID - 1),
        _update(f"tf:o:c:0:{SESSION_ID}", message_id=100),
    )
    for index, mismatch in enumerate(mismatches, start=102):
        assert asyncio.run(controller.handle(replace(mismatch, update_id=index))) == "onchain_session_binding_mismatch"
    assert gateway.quoted == []
