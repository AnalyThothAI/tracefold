"""Process root for the independent Binance USD-M Demo manual execution authority."""

from __future__ import annotations

import signal
import time
from decimal import Decimal
from threading import Event
from typing import Any, Literal, cast

from loguru import logger

from tracefold.app.repository_session import RepositorySession, repositories
from tracefold.integrations.binance_manual import BinanceManualClient
from tracefold.platform.config.models import Settings, manual_trading_availability
from tracefold.platform.config.secret_file import SecretFileError, read_secure_secret_text
from tracefold.trading import (
    ManualAttemptLeg,
    ManualExecutionPlan,
    ManualExecutionRecord,
    ManualExecutionService,
    trading_credential_fingerprint,
)

_MANUAL_EXECUTOR_LOCK = 0x5452464D  # TRFM
_MANUAL_EXECUTOR_POLL_SECONDS = 1.0


class PostgresManualExecutionStore:
    def __init__(self, repos: RepositorySession, *, account_ref: str) -> None:
        self._repos = repos
        self._account_ref = account_ref

    def initialize(
        self,
        *,
        credential_fingerprint: str,
        provider_account_fingerprint: str,
        now_ms: int,
    ) -> None:
        with self._repos.transaction():
            self._repos.trading.register_trading_account_binding(
                account_ref=self._account_ref,
                account_lane="manual",
                venue="binance_usdm_demo",
                credential_fingerprint=credential_fingerprint,
                provider_account_fingerprint=provider_account_fingerprint,
                now_ms=now_ms,
            )

    def refresh_account(self, *, equity_usd: Decimal, observed_at_ms: int) -> None:
        with self._repos.transaction():
            if not self._repos.trading.upsert_manual_account_snapshot(
                account_ref=self._account_ref,
                venue="binance_usdm_demo",
                equity_usd=equity_usd,
                observed_at_ms=observed_at_ms,
                now_ms=observed_at_ms,
            ):
                raise RuntimeError("manual_executor_account_snapshot_conflict")

    def next_intent(self) -> ManualExecutionRecord | None:
        return self._repos.trading.manual_next_execution_intent()

    def fence_entry(self, intent_id: str, *, plan: ManualExecutionPlan, now_ms: int) -> bool:
        with self._repos.transaction():
            return self._repos.trading.fence_manual_entry(intent_id, plan=plan, now_ms=now_ms)

    def record_entry(self, intent_id: str, *, receipt: dict[str, object], now_ms: int) -> bool:
        with self._repos.transaction():
            return self._repos.trading.record_manual_entry(intent_id, receipt=receipt, now_ms=now_ms)

    def begin_attempt(self, intent_id: str, *, leg: ManualAttemptLeg, now_ms: int) -> bool:
        if leg not in {"execution_setting", "entry", "take_profit", "stop_loss"}:
            raise ValueError("manual_executor_leg_invalid")
        with self._repos.transaction():
            return self._repos.trading.begin_manual_order_attempt(
                intent_id,
                leg=leg,
                now_ms=now_ms,
            )

    def record_execution_setting(self, intent_id: str, *, now_ms: int) -> bool:
        with self._repos.transaction():
            return self._repos.trading.record_manual_execution_setting(intent_id, now_ms=now_ms)

    def fence_protection(self, intent_id: str, *, leg: str, client_id: str, now_ms: int) -> bool:
        with self._repos.transaction():
            return self._repos.trading.fence_manual_protection(
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
        with self._repos.transaction():
            return self._repos.trading.record_manual_protection(
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
        with self._repos.transaction():
            return self._repos.trading.mark_manual_order_ambiguous(
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
        with self._repos.transaction():
            return self._repos.trading.reject_manual_order(
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
        with self._repos.transaction():
            return self._repos.trading.mark_manual_position_exposed(
                intent_id,
                leg=_protection_leg(leg),
                error_code=error_code,
                now_ms=now_ms,
            )


def run_manual_executor(settings: Settings) -> None:
    availability = manual_trading_availability(settings, inspect_secret_files=False)
    if not availability.requested or not availability.interaction_available:
        raise RuntimeError(f"manual_executor_unavailable:{availability.reason or 'manual_trading_disabled'}")
    key_file = settings.trading_manual_api_key_file()
    secret_file = settings.trading_manual_api_secret_file()
    if key_file is None or secret_file is None:
        raise RuntimeError("manual_executor_credentials_missing")
    try:
        api_key = read_secure_secret_text(key_file)
        api_secret = read_secure_secret_text(secret_file)
    except SecretFileError as exc:
        raise RuntimeError(f"manual_executor_credentials_{exc.code}") from None
    fingerprint = trading_credential_fingerprint(
        venue="binance_usdm_demo",
        api_key=api_key,
        api_secret=api_secret,
    )
    stop = Event()

    def request_stop(_signum: int, _frame: Any) -> None:
        stop.set()

    previous_term = signal.signal(signal.SIGTERM, request_stop)
    previous_int = signal.signal(signal.SIGINT, request_stop)
    client = BinanceManualClient(api_key=api_key, api_secret=api_secret)
    try:
        provider_account = client.account()
        with repositories(settings, role="nautilus") as repos:
            locked = repos.conn.execute(
                "SELECT pg_try_advisory_lock(%s) AS locked",
                (_MANUAL_EXECUTOR_LOCK,),
            ).fetchone()
            if locked is None or not bool(locked["locked"]):
                raise RuntimeError("manual_executor_already_running")
            store = PostgresManualExecutionStore(repos, account_ref=settings.trading.manual.account_ref)
            store.initialize(
                credential_fingerprint=fingerprint,
                provider_account_fingerprint=provider_account.provider_account_fingerprint,
                now_ms=time.time_ns() // 1_000_000,
            )
            service = ManualExecutionService(store=store, venue=client)
            while not stop.is_set():
                try:
                    result = service.turn()
                    if result != "idle":
                        logger.info("manual executor turn result={}", result)
                except Exception as exc:
                    logger.error("manual executor turn failed error={}", type(exc).__name__)
                stop.wait(_MANUAL_EXECUTOR_POLL_SECONDS)
    finally:
        client.close()
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGINT, previous_int)


def _protection_leg(value: str) -> Literal["take_profit", "stop_loss"]:
    if value not in {"take_profit", "stop_loss"}:
        raise ValueError("manual_executor_protection_leg_invalid")
    return cast(Literal["take_profit", "stop_loss"], value)


__all__ = ["PostgresManualExecutionStore", "run_manual_executor"]
