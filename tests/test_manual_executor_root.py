from __future__ import annotations

from contextlib import nullcontext

import pytest

from tracefold.app import manual_executor_root
from tracefold.app.manual_executor_root import PostgresManualExecutionStore


class _TradingRepository:
    def __init__(self, *, cutover_blocked: bool) -> None:
        self.cutover_blocked = cutover_blocked
        self.binding_registered = False

    def assert_manual_live_cutover_ready(self) -> None:
        if self.cutover_blocked:
            raise RuntimeError("manual_executor_live_cutover_blocked")

    def register_trading_account_binding(self, **_values: object) -> bool:
        self.binding_registered = True
        return True


class _Repositories:
    def __init__(self, *, cutover_blocked: bool) -> None:
        self.trading = _TradingRepository(cutover_blocked=cutover_blocked)

    def transaction(self):
        return nullcontext()


def test_live_executor_refuses_an_unsettled_demo_intent_before_binding_the_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repos = _Repositories(cutover_blocked=True)
    monkeypatch.setattr(manual_executor_root, "repositories", lambda *_args, **_kwargs: nullcontext(repos))
    store = PostgresManualExecutionStore(object(), account_ref="binance-manual-live-1")  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="manual_executor_live_cutover_blocked"):
        store.initialize(
            credential_fingerprint="a" * 64,
            provider_account_fingerprint="b" * 64,
            now_ms=1_900_000_000_000,
        )

    assert repos.trading.binding_registered is False
