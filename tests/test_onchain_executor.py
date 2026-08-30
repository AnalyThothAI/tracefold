from __future__ import annotations

import asyncio
from typing import Any

from eth_abi.abi import encode as encode_abi

from tracefold.trading import (
    OnchainExecutionIntent,
    OnchainExecutionPlan,
    OnchainExecutionService,
    OnchainExecutionState,
    OnchainQuoteRequest,
    OnchainRouteQuote,
    OnchainSignedTransaction,
    OnchainTransactionTemplate,
    onchain_wallet_fingerprint,
)

NOW = 1_900_000_000_000
WALLET = "0x7e5f4552091a69125d5dfcb7b8c2659029395bdf"
USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
TOKEN = "0x1111111111111111111111111111111111111111"
ROUTER = "0x111111125421ca6dc452d289314280a0f8842a65"
EXECUTOR = "0x2222222222222222222222222222222222222222"


def _request() -> OnchainQuoteRequest:
    return OnchainQuoteRequest(
        chain_id=1,
        input_contract=USDC,
        output_contract=TOKEN,
        input_amount_raw=10_000_000,
        slippage_bps=100,
    )


def _quote() -> OnchainRouteQuote:
    return OnchainRouteQuote(
        provider="oneinch",
        chain_id=1,
        input_contract=USDC,
        output_contract=TOKEN,
        input_amount_raw=10_000_000,
        expected_output_raw=1_000,
        minimum_output_raw=990,
        slippage_bps=100,
        latency_ms=10,
        received_at_ms=NOW,
        expires_at_ms=NOW + 30_000,
    )


def _plan() -> OnchainExecutionPlan:
    approval_data = "0x095ea7b3" + ROUTER[2:].rjust(64, "0") + hex(10_000_000)[2:].rjust(64, "0")
    swap_data = (
        "0x07ed2379"
        + encode_abi(
            ["address", "(address,address,address,address,uint256,uint256,uint256)", "bytes"],
            [EXECUTOR, (USDC, TOKEN, ROUTER, WALLET, 10_000_000, 990, 0), b"\x01"],
        ).hex()
    )
    return OnchainExecutionPlan(
        provider="oneinch",
        wallet_address=WALLET,
        wallet_fingerprint=onchain_wallet_fingerprint(WALLET),
        request=_request(),
        quote=_quote(),
        approval=OnchainTransactionTemplate(
            provider="oneinch",
            leg="approval",
            chain_id=1,
            from_address=WALLET,
            to_address=USDC,
            data=approval_data,
            gas_limit=999_999,
            gas_price=999_999,
        ),
        swap=OnchainTransactionTemplate(
            provider="oneinch",
            leg="swap",
            chain_id=1,
            from_address=WALLET,
            to_address=ROUTER,
            data=swap_data,
            gas_limit=999_999,
            gas_price=999_999,
        ),
        prepared_at_ms=NOW + 1,
        expires_at_ms=NOW + 20_000,
    )


def _intent(
    *,
    plan: OnchainExecutionPlan | None = None,
    approval: OnchainSignedTransaction | None = None,
    approval_state: str | None = None,
) -> OnchainExecutionIntent:
    return OnchainExecutionIntent(
        execution_id="11111111-1111-1111-1111-111111111111",
        session_id="22222222-2222-2222-2222-222222222222",
        actor_user_id=1,
        chat_id=1,
        interaction_message_id=1,
        provider="oneinch",
        wallet_address=WALLET,
        wallet_fingerprint=onchain_wallet_fingerprint(WALLET),
        request=_request(),
        quote=_quote(),
        state=OnchainExecutionState.CLAIMED,
        confirmation_update_id=7,
        plan=plan,
        approval_transaction=approval,
        approval_transaction_state=approval_state,
        created_at_ms=NOW,
        confirmed_at_ms=NOW + 1,
        updated_at_ms=NOW + 1,
    )


class _Store:
    def __init__(self, intent: OnchainExecutionIntent) -> None:
        self.intent = intent
        self.plans: list[OnchainExecutionPlan] = []
        self.appended: list[OnchainSignedTransaction] = []
        self.settled: list[dict[str, Any]] = []
        self.advanced: list[tuple[OnchainExecutionState, OnchainExecutionState]] = []

    def next_execution(self, *, now_ms: int) -> OnchainExecutionIntent:
        return self.intent

    def store_plan(self, _execution_id: str, *, plan: OnchainExecutionPlan, now_ms: int) -> bool:
        self.plans.append(plan)
        return True

    def append_signed(self, _execution_id: str, *, signed: OnchainSignedTransaction, now_ms: int) -> bool:
        self.appended.append(signed)
        return True

    def settle_signed(
        self,
        _execution_id: str,
        *,
        leg: str,
        state: str,
        now_ms: int,
        receipt: dict[str, Any] | None = None,
        error_code: str | None = None,
    ) -> bool:
        self.settled.append(
            {
                "leg": leg,
                "state": state,
                "now_ms": now_ms,
                "receipt": receipt,
                "error_code": error_code,
            }
        )
        return True

    def advance(
        self,
        _execution_id: str,
        *,
        expected_state: OnchainExecutionState,
        state: OnchainExecutionState,
        now_ms: int,
        error_code: str | None = None,
    ) -> bool:
        self.advanced.append((expected_state, state))
        return True


class _Provider:
    def __init__(self, plan: OnchainExecutionPlan) -> None:
        self.plan = plan
        self.calls = 0

    async def prepare_execution(self, _request: Any, *, wallet_address: str) -> OnchainExecutionPlan:
        self.calls += 1
        return self.plan


class _Rpc:
    def __init__(self) -> None:
        self.simulated: list[OnchainTransactionTemplate] = []
        self.sent: list[OnchainSignedTransaction] = []

    async def verify_chain(self) -> None:
        return None

    async def pending_nonce(self, _address: str) -> int:
        return 9

    async def gas_price(self) -> int:
        return 20

    async def estimate_gas(self, _template: OnchainTransactionTemplate) -> int:
        return 120_000

    async def simulate(self, template: OnchainTransactionTemplate) -> None:
        self.simulated.append(template)

    async def allowance(self, *, token: str, owner: str, spender: str) -> int:
        return 0

    async def send_raw_transaction(self, signed: OnchainSignedTransaction) -> str:
        self.sent.append(signed)
        return signed.transaction_hash

    async def receipt(self, _transaction_hash: str) -> None:
        return None


class _Signer:
    address = WALLET

    def __init__(self) -> None:
        self.calls: list[tuple[int, int, int]] = []

    def sign(
        self,
        template: OnchainTransactionTemplate,
        *,
        nonce: int,
        gas_limit: int,
        gas_price: int,
    ) -> OnchainSignedTransaction:
        self.calls.append((nonce, gas_limit, gas_price))
        return OnchainSignedTransaction(
            provider=template.provider,
            leg=template.leg,
            chain_id=template.chain_id,
            wallet_address=WALLET,
            nonce=nonce,
            raw_transaction="0x1234",
            transaction_hash="0x" + "a" * 64,
        )


def test_executor_simulates_and_uses_trusted_rpc_gas_before_signing() -> None:
    plan = _plan()
    store = _Store(_intent())
    provider = _Provider(plan)
    rpc = _Rpc()
    signer = _Signer()
    service = OnchainExecutionService(
        store=store,
        providers={"oneinch": provider},
        rpcs={1: rpc},
        signer=signer,
        clock_ms=lambda: NOW + 2,
    )

    result = asyncio.run(service.turn())

    assert result == "approval_submitted"
    assert store.plans == [plan]
    assert rpc.simulated == [plan.approval]
    assert signer.calls == [(9, 120_000, 20)]
    assert rpc.sent == store.appended
    assert store.advanced == [(OnchainExecutionState.CLAIMED, OnchainExecutionState.APPROVAL_SUBMITTED)]


def test_executor_replays_durable_signed_bytes_without_resigning_after_crash() -> None:
    plan = _plan()
    signed = OnchainSignedTransaction(
        provider="oneinch",
        leg="approval",
        chain_id=1,
        wallet_address=WALLET,
        nonce=9,
        raw_transaction="0x1234",
        transaction_hash="0x" + "b" * 64,
    )
    store = _Store(_intent(plan=plan, approval=signed, approval_state="SIGNED"))
    provider = _Provider(plan)
    rpc = _Rpc()
    signer = _Signer()
    service = OnchainExecutionService(
        store=store,
        providers={"oneinch": provider},
        rpcs={1: rpc},
        signer=signer,
        clock_ms=lambda: NOW + 2,
    )

    result = asyncio.run(service.turn())

    assert result == "approval_submitted"
    assert provider.calls == 0
    assert signer.calls == []
    assert rpc.simulated == []
    assert store.appended == []
    assert rpc.sent == [signed]
    assert store.advanced == [(OnchainExecutionState.CLAIMED, OnchainExecutionState.APPROVAL_SUBMITTED)]
