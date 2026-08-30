"""Process root for the independent Binance USD-M live manual execution authority."""

from __future__ import annotations

import signal
import time
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from threading import Event
from typing import Any, Literal, cast

from loguru import logger

from tracefold.app.repository_session import repositories
from tracefold.integrations.binance_manual import BinanceManualClient
from tracefold.platform.config.models import Settings, manual_trading_availability, manual_trading_profile_availability
from tracefold.platform.config.secret_file import SecretFileError, read_secure_secret_text
from tracefold.trading import (
    ManualAttemptLeg,
    ManualExecutionPlan,
    ManualExecutionRecord,
    ManualExecutionService,
    ManualManagedPositionRecord,
    ManualVenuePosition,
    trading_credential_fingerprint,
)
from tracefold.trading.storage.root import TradingRepository

_MANUAL_EXECUTOR_POLL_SECONDS = 1.0
_MANUAL_EXECUTOR_HEARTBEAT_PATH = Path("/tmp/tracefold-manual-executor-heartbeat")  # noqa: S108


class PostgresManualExecutionStore:
    def __init__(self, settings: Settings, *, account_ref: str) -> None:
        self._settings = settings
        self._account_ref = account_ref

    @contextmanager
    def _transaction(self) -> Iterator[TradingRepository]:
        with repositories(self._settings, role="nautilus") as repos, repos.transaction():
            yield repos.trading

    def initialize(
        self,
        *,
        credential_fingerprint: str,
        provider_account_fingerprint: str,
        now_ms: int,
    ) -> None:
        with self._transaction() as trading:
            trading.assert_manual_live_cutover_ready()
            trading.register_trading_account_binding(
                account_ref=self._account_ref,
                account_lane="manual",
                venue="binance_usdm_live",
                credential_fingerprint=credential_fingerprint,
                provider_account_fingerprint=provider_account_fingerprint,
                now_ms=now_ms,
            )

    def refresh_account(self, *, equity_usd: Decimal, observed_at_ms: int) -> None:
        with self._transaction() as trading:
            if not trading.upsert_manual_account_snapshot(
                account_ref=self._account_ref,
                venue="binance_usdm_live",
                equity_usd=equity_usd,
                observed_at_ms=observed_at_ms,
                now_ms=observed_at_ms,
            ):
                raise RuntimeError("manual_executor_account_snapshot_conflict")

    def next_intent(self) -> ManualExecutionRecord | None:
        with repositories(self._settings, role="nautilus") as repos:
            return repos.trading.manual_next_execution_intent(account_ref=self._account_ref)

    def next_open_position(self) -> ManualManagedPositionRecord | None:
        with repositories(self._settings, role="nautilus") as repos:
            return repos.trading.manual_next_open_position(account_ref=self._account_ref)

    def has_active_symbol(self, *, base_symbol: str, exclude_intent_id: str) -> bool:
        with repositories(self._settings, role="nautilus") as repos:
            return repos.trading.manual_has_active_symbol(
                account_ref=self._account_ref,
                base_symbol=base_symbol,
                exclude_intent_id=exclude_intent_id,
            )

    def fence_entry(self, intent_id: str, *, plan: ManualExecutionPlan, now_ms: int) -> bool:
        with self._transaction() as trading:
            return trading.fence_manual_entry(intent_id, plan=plan, now_ms=now_ms)

    def record_entry(self, intent_id: str, *, receipt: dict[str, object], now_ms: int) -> bool:
        with self._transaction() as trading:
            return trading.record_manual_entry(intent_id, receipt=receipt, now_ms=now_ms)

    def begin_attempt(self, intent_id: str, *, leg: ManualAttemptLeg, now_ms: int) -> bool:
        if leg not in {"execution_setting", "entry", "take_profit", "stop_loss"}:
            raise ValueError("manual_executor_leg_invalid")
        with self._transaction() as trading:
            return trading.begin_manual_order_attempt(
                intent_id,
                leg=leg,
                now_ms=now_ms,
            )

    def record_execution_setting(self, intent_id: str, *, now_ms: int) -> bool:
        with self._transaction() as trading:
            return trading.record_manual_execution_setting(intent_id, now_ms=now_ms)

    def fence_protection(self, intent_id: str, *, leg: str, client_id: str, now_ms: int) -> bool:
        with self._transaction() as trading:
            return trading.fence_manual_protection(
                intent_id,
                leg=_protection_leg(leg),
                client_id=client_id,
                now_ms=now_ms,
            )

    def record_protection(
        self,
        intent_id: str,
        *,
        leg: str,
        receipt: dict[str, object],
        now_ms: int,
    ) -> bool:
        with self._transaction() as trading:
            return trading.record_manual_protection(
                intent_id,
                leg=_protection_leg(leg),
                receipt=receipt,
                now_ms=now_ms,
            )

    def mark_ambiguous(
        self,
        intent_id: str,
        *,
        leg: ManualAttemptLeg,
        error_code: str,
        now_ms: int,
    ) -> bool:
        if leg not in {"execution_setting", "entry", "take_profit", "stop_loss"}:
            raise ValueError("manual_executor_leg_invalid")
        with self._transaction() as trading:
            return trading.mark_manual_order_ambiguous(
                intent_id,
                leg=leg,
                error_code=error_code,
                now_ms=now_ms,
            )

    def reject(
        self,
        intent_id: str,
        *,
        leg: ManualAttemptLeg,
        error_code: str,
        now_ms: int,
    ) -> bool:
        if leg not in {"execution_setting", "entry", "take_profit", "stop_loss"}:
            raise ValueError("manual_executor_leg_invalid")
        with self._transaction() as trading:
            return trading.reject_manual_order(
                intent_id,
                leg=leg,
                error_code=error_code,
                now_ms=now_ms,
            )

    def mark_exposed(
        self,
        intent_id: str,
        *,
        leg: str,
        error_code: str,
        now_ms: int,
    ) -> bool:
        with self._transaction() as trading:
            return trading.mark_manual_position_exposed(
                intent_id,
                leg=_protection_leg(leg),
                error_code=error_code,
                now_ms=now_ms,
            )

    def observe_position(
        self,
        intent_id: str,
        *,
        position: ManualVenuePosition,
        plan: ManualExecutionPlan,
        opened_at_ms: int,
        now_ms: int,
    ) -> bool:
        with self._transaction() as trading:
            return trading.observe_manual_position(
                intent_id,
                position=position,
                plan=plan,
                opened_at_ms=opened_at_ms,
                now_ms=now_ms,
            )

    def begin_close_attempt(self, close_id: str, *, quantity: Decimal, now_ms: int) -> bool:
        with self._transaction() as trading:
            return trading.begin_manual_close_attempt(close_id, quantity=quantity, now_ms=now_ms)

    def record_close_fill(self, close_id: str, *, receipt: dict[str, object], now_ms: int) -> bool:
        with self._transaction() as trading:
            return trading.record_manual_close_fill(close_id, receipt=receipt, now_ms=now_ms)

    def reconcile_close_fill(self, close_id: str, *, receipt: dict[str, object], now_ms: int) -> bool:
        with self._transaction() as trading:
            return trading.reconcile_manual_close_fill(close_id, receipt=receipt, now_ms=now_ms)

    def record_partial_close_reconciled(
        self,
        close_id: str,
        *,
        remaining_quantity: Decimal,
        mark_price: Decimal,
        now_ms: int,
    ) -> bool:
        with self._transaction() as trading:
            return trading.record_manual_partial_close_reconciled(
                close_id,
                remaining_quantity=remaining_quantity,
                mark_price=mark_price,
                now_ms=now_ms,
            )

    def reject_close(self, close_id: str, *, error_code: str, now_ms: int) -> bool:
        with self._transaction() as trading:
            return trading.settle_manual_close_failure(
                close_id,
                state="REJECTED",
                error_code=error_code,
                now_ms=now_ms,
            )

    def mark_close_ambiguous(self, close_id: str, *, error_code: str, now_ms: int) -> bool:
        with self._transaction() as trading:
            return trading.settle_manual_close_failure(
                close_id,
                state="AMBIGUOUS",
                error_code=error_code,
                now_ms=now_ms,
            )

    def begin_protection_cancel(
        self,
        intent_id: str,
        *,
        leg: Literal["take_profit", "stop_loss"],
        now_ms: int,
    ) -> bool:
        with self._transaction() as trading:
            return trading.begin_manual_protection_cancel(intent_id, leg=leg, now_ms=now_ms)

    def record_protection_cancelled(
        self,
        intent_id: str,
        *,
        leg: Literal["take_profit", "stop_loss"],
        now_ms: int,
    ) -> bool:
        with self._transaction() as trading:
            return trading.record_manual_protection_cancelled(intent_id, leg=leg, now_ms=now_ms)

    def close_position(
        self,
        intent_id: str,
        *,
        exit_reason: str,
        exit_price: Decimal,
        realized_pnl_usd: Decimal,
        now_ms: int,
    ) -> bool:
        with self._transaction() as trading:
            return trading.close_manual_position(
                intent_id,
                exit_reason=exit_reason,
                exit_price=exit_price,
                realized_pnl_usd=realized_pnl_usd,
                now_ms=now_ms,
            )

    def mark_position_review(self, intent_id: str, *, error_code: str, now_ms: int) -> bool:
        with self._transaction() as trading:
            return trading.mark_manual_position_review(
                intent_id,
                error_code=error_code,
                now_ms=now_ms,
            )


def run_manual_executor(settings: Settings) -> None:
    _MANUAL_EXECUTOR_HEARTBEAT_PATH.unlink(missing_ok=True)
    availability = manual_trading_availability(settings, inspect_secret_files=False)
    if not availability.requested or not availability.interaction_available:
        raise RuntimeError(f"manual_executor_unavailable:{availability.reason or 'manual_trading_disabled'}")
    profiles = tuple(profile for profile in settings.trading.telegram_profiles if profile.manual.enabled)
    configured: list[tuple[Any, BinanceManualClient, str]] = []
    for profile in profiles:
        profile_availability = manual_trading_profile_availability(settings, profile, inspect_secret_files=False)
        if not profile_availability.interaction_available:
            raise RuntimeError(
                f"manual_executor_profile_unavailable:{profile.user_id}:{profile_availability.reason or 'invalid'}"
            )
        key_file = settings.trading_manual_api_key_file(profile)
        secret_file = settings.trading_manual_api_secret_file(profile)
        if key_file is None or secret_file is None:
            raise RuntimeError("manual_executor_credentials_missing")
        try:
            api_key = read_secure_secret_text(key_file)
            api_secret = read_secure_secret_text(secret_file)
        except SecretFileError as exc:
            raise RuntimeError(f"manual_executor_credentials_{exc.code}") from None
        fingerprint = trading_credential_fingerprint(
            venue="binance_usdm_live",
            api_key=api_key,
            api_secret=api_secret,
        )
        configured.append((profile, BinanceManualClient(api_key=api_key, api_secret=api_secret), fingerprint))
    stop = Event()

    def request_stop(_signum: int, _frame: Any) -> None:
        stop.set()

    previous_term = signal.signal(signal.SIGTERM, request_stop)
    previous_int = signal.signal(signal.SIGINT, request_stop)
    try:
        services: list[tuple[int, ManualExecutionService]] = []
        for profile, client, fingerprint in configured:
            provider_account = client.account()
            store = PostgresManualExecutionStore(settings, account_ref=profile.manual.account_ref)
            store.initialize(
                credential_fingerprint=fingerprint,
                provider_account_fingerprint=provider_account.provider_account_fingerprint,
                now_ms=time.time_ns() // 1_000_000,
            )
            services.append((profile.user_id, ManualExecutionService(store=store, venue=client)))
        while not stop.is_set():
            for user_id, service in services:
                try:
                    result = service.turn()
                    if result != "idle":
                        logger.info("manual executor turn user={} result={}", user_id, result)
                except Exception as exc:
                    logger.error("manual executor turn failed user={} error={}", user_id, type(exc).__name__)
                    raise
            _record_manual_executor_heartbeat()
            stop.wait(_MANUAL_EXECUTOR_POLL_SECONDS)
    finally:
        _MANUAL_EXECUTOR_HEARTBEAT_PATH.unlink(missing_ok=True)
        for _profile, client, _fingerprint in configured:
            client.close()
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGINT, previous_int)


def _record_manual_executor_heartbeat() -> None:
    heartbeat_ms = time.time_ns() // 1_000_000
    pending = _MANUAL_EXECUTOR_HEARTBEAT_PATH.with_suffix(".pending")
    pending.write_text(f"{heartbeat_ms}\n", encoding="ascii")
    pending.replace(_MANUAL_EXECUTOR_HEARTBEAT_PATH)


def _protection_leg(value: str) -> Literal["take_profit", "stop_loss"]:
    if value not in {"take_profit", "stop_loss"}:
        raise ValueError("manual_executor_protection_leg_invalid")
    return cast(Literal["take_profit", "stop_loss"], value)


__all__ = ["PostgresManualExecutionStore", "run_manual_executor"]
