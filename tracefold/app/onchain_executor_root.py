"""Process root for independent per-Telegram-user EVM execution profiles."""

from __future__ import annotations

import asyncio
import signal
import time
from contextlib import suppress
from pathlib import Path
from threading import Event
from typing import Any

from loguru import logger

from tracefold.app.repository_session import repositories
from tracefold.integrations.onchain import (
    EvmJsonRpcClient,
    EvmPrivateKeySigner,
    OkxOnchainClient,
    OneInchOnchainClient,
)
from tracefold.platform.config.models import (
    Settings,
    onchain_execution_settlement_supported,
    onchain_trading_availability,
    onchain_trading_profile_availability,
)
from tracefold.platform.config.secret_file import SecretFileError, read_secure_secret_text
from tracefold.trading import (
    OnchainExecutionIntent,
    OnchainExecutionPlan,
    OnchainExecutionService,
    OnchainExecutionState,
    OnchainProvider,
    OnchainSignedTransaction,
    onchain_wallet_fingerprint,
)

_ONCHAIN_EXECUTOR_POLL_SECONDS = 1.0
_ONCHAIN_EXECUTOR_HEARTBEAT_PATH = Path("/tmp/tracefold-onchain-executor-heartbeat")  # noqa: S108


class PostgresOnchainExecutionStore:
    def __init__(self, settings: Settings, *, actor_user_id: int, wallet_fingerprint: str) -> None:
        self._settings = settings
        self._actor_user_id = actor_user_id
        self._wallet_fingerprint = wallet_fingerprint

    def next_execution(self, *, now_ms: int) -> OnchainExecutionIntent | None:
        with repositories(self._settings, role="onchain") as repos, repos.transaction():
            return repos.trading.claim_next_onchain_execution(
                actor_user_id=self._actor_user_id,
                wallet_fingerprint=self._wallet_fingerprint,
                now_ms=now_ms,
            )

    def heartbeat(self, *, wallet_fingerprint: str, now_ms: int) -> bool:
        with repositories(self._settings, role="onchain") as repos, repos.transaction():
            return repos.trading.record_onchain_executor_heartbeat(
                wallet_fingerprint=wallet_fingerprint,
                now_ms=now_ms,
            )

    def store_plan(self, execution_id: str, *, plan: OnchainExecutionPlan, now_ms: int) -> bool:
        with repositories(self._settings, role="onchain") as repos, repos.transaction():
            return repos.trading.store_onchain_execution_plan(execution_id, plan=plan, now_ms=now_ms)

    def append_signed(
        self,
        execution_id: str,
        *,
        signed: OnchainSignedTransaction,
        now_ms: int,
    ) -> bool:
        with repositories(self._settings, role="onchain") as repos, repos.transaction():
            return repos.trading.append_onchain_signed_transaction(
                execution_id,
                signed=signed,
                now_ms=now_ms,
            )

    def settle_signed(self, execution_id: str, **values: Any) -> bool:
        with repositories(self._settings, role="onchain") as repos, repos.transaction():
            return repos.trading.settle_onchain_signed_transaction(execution_id, **values)

    def advance(
        self,
        execution_id: str,
        *,
        expected_state: OnchainExecutionState,
        state: OnchainExecutionState,
        now_ms: int,
        error_code: str | None = None,
    ) -> bool:
        with repositories(self._settings, role="onchain") as repos, repos.transaction():
            return repos.trading.advance_onchain_execution(
                execution_id,
                expected_state=expected_state,
                state=state,
                now_ms=now_ms,
                error_code=error_code,
            )


def run_onchain_executor(settings: Settings) -> None:
    _ONCHAIN_EXECUTOR_HEARTBEAT_PATH.unlink(missing_ok=True)
    availability = onchain_trading_availability(settings, inspect_secret_files=False)
    if not availability.requested or not availability.execution_available:
        raise RuntimeError(f"onchain_executor_unavailable:{availability.execution_reason or availability.reason}")
    onchain = settings.trading.onchain
    runtimes: list[tuple[int, PostgresOnchainExecutionStore, OnchainExecutionService, str]] = []
    closeables: list[Any] = []
    try:
        for profile in settings.trading.telegram_profiles:
            if not profile.onchain.enabled:
                continue
            profile_availability = onchain_trading_profile_availability(settings, profile, inspect_secret_files=False)
            if not profile_availability.execution_available:
                continue
            wallet = profile.onchain.wallet
            key_path = settings.trading_onchain_wallet_private_key_file(profile)
            if key_path is None or wallet.address is None:
                raise RuntimeError("onchain_executor_wallet_configuration_missing")
            try:
                signer = EvmPrivateKeySigner(read_secure_secret_text(key_path))
            except SecretFileError as exc:
                raise RuntimeError(f"onchain_executor_wallet_{exc.code}") from None
            if signer.address != wallet.address:
                raise RuntimeError(f"onchain_executor_wallet_address_mismatch:{profile.user_id}")
            providers: dict[OnchainProvider, Any] = {}
            if "okx" in profile_availability.configured_quote_providers:
                providers["okx"] = OkxOnchainClient(
                    api_key=_read_secret(settings.trading_onchain_okx_api_key_file(profile), "okx_api_key"),
                    api_secret=_read_secret(settings.trading_onchain_okx_api_secret_file(profile), "okx_api_secret"),
                    passphrase=_read_secret(settings.trading_onchain_okx_passphrase_file(profile), "okx_passphrase"),
                )
            if "oneinch" in profile_availability.configured_quote_providers:
                providers["oneinch"] = OneInchOnchainClient(
                    api_key=_read_secret(settings.trading_onchain_oneinch_api_key_file(profile), "oneinch_api_key"),
                )
            rpcs = {
                asset.chain_id: EvmJsonRpcClient(rpc_url=asset.rpc_url, chain_id=asset.chain_id)
                for asset in onchain.settlement_assets
                if asset.rpc_url is not None and onchain_execution_settlement_supported(asset)
            }
            fingerprint = onchain_wallet_fingerprint(signer.address)
            store = PostgresOnchainExecutionStore(
                settings,
                actor_user_id=profile.user_id,
                wallet_fingerprint=fingerprint,
            )
            runtimes.append(
                (
                    profile.user_id,
                    store,
                    OnchainExecutionService(store=store, providers=providers, rpcs=rpcs, signer=signer),
                    fingerprint,
                )
            )
            closeables.extend((*providers.values(), *rpcs.values()))
        if not runtimes:
            raise RuntimeError("onchain_executor_no_executable_profile")
        asyncio.run(_run_loop(runtimes))
    finally:
        asyncio.run(_close_all(tuple(closeables)))
        _ONCHAIN_EXECUTOR_HEARTBEAT_PATH.unlink(missing_ok=True)


async def _run_loop(
    runtimes: list[tuple[int, PostgresOnchainExecutionStore, OnchainExecutionService, str]],
) -> None:
    stop = Event()
    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGTERM, signal.SIGINT):
        with suppress(NotImplementedError):
            loop.add_signal_handler(signal_name, stop.set)
    while not stop.is_set():
        heartbeat_at_ms = time.time_ns() // 1_000_000
        for user_id, store, execution, wallet_fingerprint in runtimes:
            if not store.heartbeat(wallet_fingerprint=wallet_fingerprint, now_ms=heartbeat_at_ms):
                raise RuntimeError(f"onchain_executor_heartbeat_conflict:{user_id}")
            result = await execution.turn()
            if result != "idle":
                logger.info("onchain executor turn user={} result={}", user_id, result)
        _record_heartbeat(heartbeat_at_ms)
        await asyncio.sleep(_ONCHAIN_EXECUTOR_POLL_SECONDS)


async def _close_all(values: tuple[Any, ...]) -> None:
    if values:
        await asyncio.gather(*(value.close() for value in values), return_exceptions=True)


def _read_secret(path: Path | None, label: str) -> str:
    if path is None:
        raise RuntimeError(f"onchain_executor_{label}_path_missing")
    try:
        return read_secure_secret_text(path)
    except SecretFileError as exc:
        raise RuntimeError(f"onchain_executor_{label}_{exc.code}") from None


def _record_heartbeat(value: int) -> None:
    pending = _ONCHAIN_EXECUTOR_HEARTBEAT_PATH.with_suffix(".pending")
    pending.write_text(f"{value}\n", encoding="ascii")
    pending.replace(_ONCHAIN_EXECUTOR_HEARTBEAT_PATH)


__all__ = ["PostgresOnchainExecutionStore", "run_onchain_executor"]
