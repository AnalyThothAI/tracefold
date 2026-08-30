"""App composition for Telegram-first onchain asset resolution and route analysis."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from typing import Any

from tracefold.app.onchain_trading import (
    OnchainCandidateResult,
    OnchainQuoteResult,
    OnchainTelegramTradingController,
)
from tracefold.app.workers.wiring.database import WorkerTradingDatabase
from tracefold.app.workers.wiring.manual_trading import ManualTradingBotAdapter
from tracefold.integrations.onchain.binance import BinanceOnchainClient
from tracefold.integrations.onchain.dexscreener import DexScreenerOnchainDiscoveryClient
from tracefold.integrations.onchain.okx import OkxOnchainClient
from tracefold.integrations.onchain.oneinch import OneInchOnchainClient
from tracefold.news import TelegramManualTradeProjectionV1
from tracefold.platform.config.models import (
    Settings,
    TradingTelegramProfileSettings,
    onchain_execution_settlement_supported,
    onchain_trading_profile_availability,
)
from tracefold.platform.config.secret_file import SecretFileError, read_secure_secret_text
from tracefold.trading import (
    OnchainAnalysisSession,
    OnchainAssetCandidate,
    OnchainExecutionIntent,
    OnchainNewsSource,
    OnchainProviderToken,
    OnchainProviderUnavailable,
    OnchainQuoteRequest,
    OnchainTelegramEditEffect,
    OnchainTelegramEditPayload,
    analyze_onchain_routes,
    resolve_onchain_candidates,
)


def onchain_sources_from_news_projection(
    projection: TelegramManualTradeProjectionV1,
    *,
    message_id: int,
    target_sha256: str,
) -> tuple[OnchainNewsSource, ...]:
    """Project only the ticker facts rendered on the immutable sent Telegram card."""

    if projection.projection_version != "telegram_manual_trade_projection_v1":
        return ()
    if projection.final_decision not in {"push", "escalate"} or projection.degraded:
        return ()
    try:
        return tuple(
            OnchainNewsSource(
                news_event_id=projection.event_id,
                delivery_target_sha256=target_sha256,
                delivery_message_id=message_id,
                headline_zh=projection.title_zh,
                ticker=ticker,
                source_observed_at_ms=projection.opened_at_ms,
            )
            for ticker in projection.displayed_assets
        )
    except (TypeError, ValueError):
        return ()


def onchain_sources_from_development_test_news(
    row: Mapping[str, Any],
    *,
    message_id: int,
    target_sha256: str,
) -> tuple[OnchainNewsSource, ...]:
    """Map only an explicitly created, unexpired onchain test fixture."""

    if row.get("test_kind") != "onchain" or row.get("delivery_target_sha256") != target_sha256:
        return ()
    targets = row.get("displayed_targets")
    if not isinstance(targets, list | tuple):
        return ()
    try:
        return tuple(
            OnchainNewsSource(
                news_event_id=f"development-test:{row['source_id']}",
                delivery_target_sha256=target_sha256,
                delivery_message_id=message_id,
                headline_zh=str(row["headline_zh"]),
                ticker=str(target),
                source_observed_at_ms=int(row["source_observed_at_ms"]),
            )
            for target in targets
        )
    except (KeyError, TypeError, ValueError):
        return ()


class OnchainTradingRepositoryAdapter:
    def __init__(self, database: WorkerTradingDatabase, *, target_sha256: str) -> None:
        self._database = database
        self._target_sha256 = target_sha256

    async def sources_for_message(self, message_id: int) -> tuple[OnchainNewsSource, ...]:
        def read(repos: Any) -> tuple[OnchainNewsSource, ...]:
            projection = repos.news.telegram_manual_trade_projection(
                message_id=message_id,
                target_sha256=self._target_sha256,
            )
            if projection is not None:
                return onchain_sources_from_news_projection(
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
            return onchain_sources_from_development_test_news(
                test_row,
                message_id=message_id,
                target_sha256=self._target_sha256,
            )

        return await self._database.read("onchain_source", read, timeout_seconds=3.0)

    async def begin_session(self, **values: Any) -> tuple[OnchainAnalysisSession, bool]:
        return await self._database.tx(
            "onchain_begin_session",
            lambda repos: repos.trading.begin_onchain_analysis_session(**values),
            timeout_seconds=3.0,
        )

    async def begin_interaction_reply(self, session_id: str, *, now_ms: int) -> bool:
        return await self._database.tx(
            "onchain_begin_reply",
            lambda repos: repos.trading.begin_onchain_interaction_reply(session_id, now_ms=now_ms),
            timeout_seconds=3.0,
        )

    async def attach_interaction_message(self, session_id: str, *, message_id: int, now_ms: int) -> bool:
        return await self._database.tx(
            "onchain_attach_reply",
            lambda repos: repos.trading.attach_onchain_interaction_message(
                session_id,
                message_id=message_id,
                now_ms=now_ms,
            ),
            timeout_seconds=3.0,
        )

    async def mark_interaction_reply_ambiguous(
        self,
        session_id: str,
        *,
        error_code: str,
        now_ms: int,
    ) -> bool:
        return await self._database.tx(
            "onchain_reply_ambiguous",
            lambda repos: repos.trading.mark_onchain_interaction_reply_ambiguous(
                session_id,
                error_code=error_code,
                now_ms=now_ms,
            ),
            timeout_seconds=3.0,
        )

    async def get_session(self, session_id: str) -> OnchainAnalysisSession | None:
        return await self._database.read(
            "onchain_get_session",
            lambda repos: repos.trading.onchain_analysis_session(session_id),
            timeout_seconds=3.0,
        )

    async def begin_execution(self, **values: Any) -> tuple[OnchainExecutionIntent, bool]:
        return await self._database.tx(
            "onchain_begin_execution",
            lambda repos: repos.trading.begin_onchain_execution(**values),
            timeout_seconds=3.0,
        )

    async def execution_for_session(self, session_id: str) -> OnchainExecutionIntent | None:
        return await self._database.read(
            "onchain_execution_status",
            lambda repos: repos.trading.onchain_execution_for_session(session_id),
            timeout_seconds=3.0,
        )

    async def executor_available(self, *, wallet_fingerprint: str, now_ms: int) -> bool:
        return await self._database.read(
            "onchain_executor_available",
            lambda repos: repos.trading.onchain_executor_available(
                wallet_fingerprint=wallet_fingerprint,
                now_ms=now_ms,
            ),
            timeout_seconds=3.0,
        )

    async def confirm_execution(self, session_id: str, *, update_id: int, now_ms: int) -> bool:
        return await self._database.tx(
            "onchain_confirm_execution",
            lambda repos: repos.trading.confirm_onchain_execution(
                session_id,
                update_id=update_id,
                now_ms=now_ms,
            ),
            timeout_seconds=3.0,
        )

    async def cancel_execution(self, session_id: str, *, update_id: int, now_ms: int) -> bool:
        return await self._database.tx(
            "onchain_cancel_execution",
            lambda repos: repos.trading.cancel_onchain_execution(
                session_id,
                update_id=update_id,
                now_ms=now_ms,
            ),
            timeout_seconds=3.0,
        )

    async def begin_edit(
        self,
        session_id: str,
        *,
        update_id: int,
        payload: OnchainTelegramEditPayload,
        result_code: str,
        now_ms: int,
    ) -> OnchainTelegramEditEffect:
        return await self._database.tx(
            "onchain_begin_edit",
            lambda repos: repos.trading.begin_onchain_telegram_edit(
                session_id,
                update_id=update_id,
                payload=payload,
                result_code=result_code,
                now_ms=now_ms,
            ),
            timeout_seconds=3.0,
        )

    async def begin_resolution(
        self,
        session_id: str,
        *,
        ticker: str,
        now_ms: int,
    ) -> OnchainAnalysisSession | None:
        return await self._database.tx(
            "onchain_begin_resolution",
            lambda repos: repos.trading.begin_onchain_resolution(
                session_id,
                ticker=ticker,
                now_ms=now_ms,
            ),
            timeout_seconds=3.0,
        )

    async def set_candidates(
        self,
        session_id: str,
        *,
        candidates: tuple[OnchainAssetCandidate, ...],
        provider_errors: tuple[str, ...],
        now_ms: int,
    ) -> OnchainAnalysisSession | None:
        return await self._database.tx(
            "onchain_set_candidates",
            lambda repos: repos.trading.set_onchain_candidates(
                session_id,
                candidates=candidates,
                provider_errors=provider_errors,
                now_ms=now_ms,
            ),
            timeout_seconds=3.0,
        )

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
        return await self._database.tx(
            "onchain_set_candidates_edit",
            lambda repos: repos.trading.set_onchain_candidates_and_begin_edit(
                session_id,
                candidates=candidates,
                provider_errors=provider_errors,
                update_id=update_id,
                payload=payload,
                result_code=result_code,
                now_ms=now_ms,
            ),
            timeout_seconds=3.0,
        )

    async def begin_quote(
        self,
        session_id: str,
        *,
        candidate_index: int | None,
        now_ms: int,
    ) -> OnchainAnalysisSession | None:
        return await self._database.tx(
            "onchain_begin_quote",
            lambda repos: repos.trading.begin_onchain_quote(
                session_id,
                candidate_index=candidate_index,
                now_ms=now_ms,
            ),
            timeout_seconds=3.0,
        )

    async def set_analysis(
        self,
        session_id: str,
        *,
        result: OnchainQuoteResult,
        now_ms: int,
    ) -> OnchainAnalysisSession | None:
        return await self._database.tx(
            "onchain_set_analysis",
            lambda repos: repos.trading.set_onchain_analysis(
                session_id,
                analysis=result.analysis,
                provider_errors=result.provider_errors,
                now_ms=now_ms,
            ),
            timeout_seconds=3.0,
        )

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
        return await self._database.tx(
            "onchain_set_analysis_edit",
            lambda repos: repos.trading.set_onchain_analysis_and_begin_edit(
                session_id,
                analysis=result.analysis,
                provider_errors=result.provider_errors,
                update_id=update_id,
                payload=payload,
                result_code=result_code,
                now_ms=now_ms,
            ),
            timeout_seconds=3.0,
        )

    async def cancel(self, session_id: str, *, now_ms: int) -> bool:
        return await self._database.tx(
            "onchain_cancel",
            lambda repos: repos.trading.cancel_onchain_analysis(session_id, now_ms=now_ms),
            timeout_seconds=3.0,
        )

    async def cancel_and_begin_edit(
        self,
        session_id: str,
        *,
        update_id: int,
        payload: OnchainTelegramEditPayload,
        result_code: str,
        now_ms: int,
    ) -> OnchainTelegramEditEffect | None:
        return await self._database.tx(
            "onchain_cancel_edit",
            lambda repos: repos.trading.cancel_onchain_analysis_and_begin_edit(
                session_id,
                update_id=update_id,
                payload=payload,
                result_code=result_code,
                now_ms=now_ms,
            ),
            timeout_seconds=3.0,
        )

    async def edit_effect(
        self,
        session_id: str,
        *,
        update_id: int,
    ) -> OnchainTelegramEditEffect | None:
        return await self._database.read(
            "onchain_edit_effect",
            lambda repos: repos.trading.onchain_telegram_edit_effect(
                session_id,
                update_id=update_id,
            ),
            timeout_seconds=3.0,
        )

    async def settle_edit_sent(self, session_id: str, *, update_id: int, now_ms: int) -> bool:
        return await self._database.tx(
            "onchain_edit_sent",
            lambda repos: repos.trading.settle_onchain_telegram_edit_sent(
                session_id,
                update_id=update_id,
                now_ms=now_ms,
            ),
            timeout_seconds=3.0,
        )

    async def settle_edit_ambiguous(
        self,
        session_id: str,
        *,
        update_id: int,
        error_code: str,
        now_ms: int,
    ) -> bool:
        return await self._database.tx(
            "onchain_edit_ambiguous",
            lambda repos: repos.trading.settle_onchain_telegram_edit_ambiguous(
                session_id,
                update_id=update_id,
                error_code=error_code,
                now_ms=now_ms,
            ),
            timeout_seconds=3.0,
        )


class OnchainRouteGateway:
    """Collect provider facts concurrently and normalize them before domain ranking."""

    def __init__(
        self,
        *,
        providers: Mapping[str, Any],
        discovery_providers: Mapping[str, Any] | None = None,
        discovery_chain_ids: tuple[int, ...] | None = None,
        settlement_assets: Mapping[int, Any],
        slippage_bps: int,
        clock_ms: Any | None = None,
    ) -> None:
        self._quote_providers = dict(providers)
        self._discovery_providers = dict(discovery_providers or providers)
        self._discovery_chain_ids = tuple(discovery_chain_ids or settlement_assets)
        self._settlement_assets = dict(settlement_assets)
        self._slippage_bps = int(slippage_bps)
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)

    async def close(self) -> None:
        values = (*self._quote_providers.values(), *self._discovery_providers.values())
        unique = {id(provider): provider for provider in values}
        await asyncio.gather(*(provider.close() for provider in unique.values()))

    async def resolve(self, ticker: str) -> OnchainCandidateResult:
        chain_ids = self._discovery_chain_ids
        outcomes = await asyncio.gather(
            *(provider.search_tokens(ticker, chain_ids=chain_ids) for provider in self._discovery_providers.values()),
            return_exceptions=True,
        )
        observations: list[OnchainProviderToken] = []
        errors: list[str] = []
        for outcome in outcomes:
            if isinstance(outcome, OnchainProviderUnavailable):
                errors.append(outcome.code)
            elif isinstance(outcome, BaseException):
                errors.append("onchain_provider_unexpected")
            else:
                observations.extend(outcome)
        return OnchainCandidateResult(
            candidates=resolve_onchain_candidates(ticker, tuple(observations))[:6],
            provider_errors=tuple(dict.fromkeys(errors)),
        )

    async def quote(self, candidate: OnchainAssetCandidate) -> OnchainQuoteResult:
        settlement = self._settlement_assets.get(candidate.chain_id)
        if settlement is None or settlement.contract_address == candidate.contract_address:
            analysis = analyze_onchain_routes((), now_ms=int(self._clock_ms()))
            return OnchainQuoteResult(
                analysis=analysis,
                settlement_symbol="",
                settlement_decimals=0,
                output_decimals=candidate.decimals,
                provider_errors=("onchain_settlement_asset_unavailable",),
            )
        request = OnchainQuoteRequest(
            chain_id=candidate.chain_id,
            input_contract=settlement.contract_address,
            output_contract=candidate.contract_address,
            input_amount_raw=settlement.quote_amount_raw,
            slippage_bps=self._slippage_bps,
        )
        outcomes = await asyncio.gather(
            *(provider.quote(request) for provider in self._quote_providers.values()),
            return_exceptions=True,
        )
        quotes = []
        errors: list[str] = []
        for outcome in outcomes:
            if isinstance(outcome, OnchainProviderUnavailable):
                errors.append(outcome.code)
            elif isinstance(outcome, BaseException):
                errors.append("onchain_provider_unexpected")
            else:
                quotes.append(outcome)
        return OnchainQuoteResult(
            analysis=analyze_onchain_routes(tuple(quotes), now_ms=int(self._clock_ms())),
            settlement_symbol=settlement.symbol,
            settlement_decimals=settlement.decimals,
            output_decimals=candidate.decimals,
            provider_errors=tuple(dict.fromkeys(errors)),
        )


class _UnavailableOnchainClient:
    """Keep an enabled-but-unconfigured provider visible without blocking peers."""

    def __init__(self, code: str) -> None:
        self._code = code

    async def close(self) -> None:
        return None

    async def search_tokens(self, _ticker: str, *, chain_ids: tuple[int, ...]) -> tuple[OnchainProviderToken, ...]:
        del chain_ids
        raise OnchainProviderUnavailable(self._code)

    async def quote(self, _request: OnchainQuoteRequest) -> Any:
        raise OnchainProviderUnavailable(self._code)


def wire_onchain_controller(
    *,
    settings: Settings,
    profile: TradingTelegramProfileSettings,
    database: WorkerTradingDatabase,
    bot: ManualTradingBotAdapter,
    target_sha256: str,
) -> OnchainTelegramTradingController | None:
    availability = onchain_trading_profile_availability(settings, profile, inspect_secret_files=False)
    if not availability.requested:
        return None
    if not availability.interaction_available:
        raise RuntimeError(f"onchain_trading_unavailable:{availability.reason or 'configuration_invalid'}")
    providers: dict[str, Any] = {}
    onchain = settings.trading.onchain
    account = profile.onchain
    if account.providers.okx.enabled:
        try:
            providers["okx"] = OkxOnchainClient(
                api_key=read_secure_secret_text(_required_path(settings.trading_onchain_okx_api_key_file(profile))),
                api_secret=read_secure_secret_text(
                    _required_path(settings.trading_onchain_okx_api_secret_file(profile))
                ),
                passphrase=read_secure_secret_text(
                    _required_path(settings.trading_onchain_okx_passphrase_file(profile))
                ),
            )
        except (RuntimeError, SecretFileError) as exc:
            providers["okx"] = _UnavailableOnchainClient(_credential_error("okx", exc))
    if account.providers.oneinch.enabled:
        try:
            providers["oneinch"] = OneInchOnchainClient(
                api_key=read_secure_secret_text(_required_path(settings.trading_onchain_oneinch_api_key_file(profile))),
            )
        except (RuntimeError, SecretFileError) as exc:
            providers["oneinch"] = _UnavailableOnchainClient(_credential_error("oneinch", exc))
    if account.providers.binance.enabled:
        providers["binance"] = BinanceOnchainClient()
    discovery_providers = {
        **providers,
        "dexscreener": DexScreenerOnchainDiscoveryClient(
            rpc_urls={asset.chain_id: asset.rpc_url for asset in onchain.settlement_assets if asset.rpc_url}
        ),
    }
    repository = OnchainTradingRepositoryAdapter(database, target_sha256=target_sha256)
    gateway = OnchainRouteGateway(
        providers=providers,
        discovery_providers=discovery_providers,
        discovery_chain_ids=onchain.chain_ids,
        settlement_assets={asset.chain_id: asset for asset in onchain.settlement_assets},
        slippage_bps=onchain.slippage_bps,
    )
    return OnchainTelegramTradingController(
        repository=repository,
        gateway=gateway,
        bot=bot,
        wallet_address=account.wallet.address,
        execution_assets={
            asset.chain_id: (asset.symbol, asset.decimals, asset.quote_amount)
            for asset in onchain.settlement_assets
            if asset.rpc_url is not None and onchain_execution_settlement_supported(asset)
        },
        execution_available=availability.execution_available,
        executable_providers=availability.executable_providers,
    )


def _required_path(value: Any) -> Any:
    if value is None:
        raise RuntimeError("onchain_secret_path_missing")
    return value


def _credential_error(provider: str, exc: RuntimeError | SecretFileError) -> str:
    reason = exc.code if isinstance(exc, SecretFileError) else "path_missing"
    return f"{provider}_credentials_{reason}"


__all__ = [
    "OnchainRouteGateway",
    "OnchainTradingRepositoryAdapter",
    "onchain_sources_from_development_test_news",
    "onchain_sources_from_news_projection",
    "wire_onchain_controller",
]
