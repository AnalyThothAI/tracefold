"""Advance one confirmed onchain execution through the shared manual EVM wallet."""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any, Protocol

from .onchain import OnchainProvider, OnchainProviderUnavailable
from .onchain_execution import (
    OnchainExecutionIntent,
    OnchainExecutionPlan,
    OnchainExecutionState,
    OnchainSignedTransaction,
    OnchainTransactionTemplate,
    decode_erc20_approval,
    validate_onchain_execution_plan,
)


class OnchainExecutionStore(Protocol):
    def next_execution(self, *, now_ms: int) -> OnchainExecutionIntent | None: ...

    def store_plan(self, execution_id: str, *, plan: OnchainExecutionPlan, now_ms: int) -> bool: ...

    def append_signed(
        self,
        execution_id: str,
        *,
        signed: OnchainSignedTransaction,
        now_ms: int,
    ) -> bool: ...

    def settle_signed(
        self,
        execution_id: str,
        *,
        leg: str,
        state: str,
        now_ms: int,
        receipt: dict[str, Any] | None = None,
        error_code: str | None = None,
    ) -> bool: ...

    def advance(
        self,
        execution_id: str,
        *,
        expected_state: OnchainExecutionState,
        state: OnchainExecutionState,
        now_ms: int,
        error_code: str | None = None,
    ) -> bool: ...


class OnchainExecutionProvider(Protocol):
    async def prepare_execution(self, request: Any, *, wallet_address: str) -> OnchainExecutionPlan: ...


class OnchainExecutionRpc(Protocol):
    async def verify_chain(self) -> None: ...

    async def pending_nonce(self, address: str) -> int: ...

    async def gas_price(self) -> int: ...

    async def estimate_gas(self, template: OnchainTransactionTemplate) -> int: ...

    async def simulate(self, template: OnchainTransactionTemplate) -> None: ...

    async def allowance(self, *, token: str, owner: str, spender: str) -> int: ...

    async def send_raw_transaction(self, signed: OnchainSignedTransaction) -> str: ...

    async def receipt(self, transaction_hash: str) -> Mapping[str, Any] | None: ...


class OnchainExecutionSigner(Protocol):
    address: str

    def sign(
        self,
        template: OnchainTransactionTemplate,
        *,
        nonce: int,
        gas_limit: int,
        gas_price: int,
    ) -> OnchainSignedTransaction: ...


class OnchainExecutionService:
    """One non-blocking turn over the durable onchain execution state machine."""

    def __init__(
        self,
        *,
        store: OnchainExecutionStore,
        providers: Mapping[OnchainProvider, OnchainExecutionProvider],
        rpcs: Mapping[int, OnchainExecutionRpc],
        signer: OnchainExecutionSigner,
        clock_ms: Any | None = None,
    ) -> None:
        self._store = store
        self._providers = dict(providers)
        self._rpcs = dict(rpcs)
        self._signer = signer
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._verified_chains: set[int] = set()

    async def turn(self) -> str:
        now_ms = int(self._clock_ms())
        intent = self._store.next_execution(now_ms=now_ms)
        if intent is None:
            return "idle"
        if intent.wallet_address != self._signer.address:
            return self._fail(intent, "onchain_executor_wallet_mismatch", now_ms=now_ms)
        rpc = self._rpcs.get(intent.request.chain_id)
        provider = self._providers.get(intent.provider)
        if rpc is None or provider is None:
            return self._fail(intent, "onchain_executor_route_unavailable", now_ms=now_ms)
        if intent.request.chain_id not in self._verified_chains:
            await rpc.verify_chain()
            self._verified_chains.add(intent.request.chain_id)

        if intent.state is OnchainExecutionState.CLAIMED:
            return await self._claimed(intent, provider=provider, rpc=rpc, now_ms=now_ms)
        if intent.state is OnchainExecutionState.APPROVAL_SUBMITTED:
            return await self._approval_submitted(intent, rpc=rpc, now_ms=now_ms)
        if intent.state is OnchainExecutionState.SWAP_SUBMITTED:
            return await self._swap_submitted(intent, rpc=rpc, now_ms=now_ms)
        raise RuntimeError("onchain_executor_state_invalid")

    async def _claimed(
        self,
        intent: OnchainExecutionIntent,
        *,
        provider: OnchainExecutionProvider,
        rpc: OnchainExecutionRpc,
        now_ms: int,
    ) -> str:
        plan = intent.plan
        if plan is None:
            try:
                plan = await provider.prepare_execution(intent.request, wallet_address=intent.wallet_address)
                validate_onchain_execution_plan(intent, plan, now_ms=int(self._clock_ms()))
            except (OnchainProviderUnavailable, ValueError) as exc:
                code = exc.code if isinstance(exc, OnchainProviderUnavailable) else str(exc)
                return self._fail(intent, code, now_ms=int(self._clock_ms()))
            if not self._store.store_plan(intent.execution_id, plan=plan, now_ms=int(self._clock_ms())):
                raise RuntimeError("onchain_executor_plan_fence_conflict")

        approval = plan.approval
        if approval is not None:
            spender, approval_amount = decode_erc20_approval(approval.data)
            if approval_amount != intent.request.input_amount_raw:
                return self._fail(intent, "onchain_approval_amount_invalid", now_ms=int(self._clock_ms()))
            allowance = await rpc.allowance(
                token=intent.request.input_contract,
                owner=intent.wallet_address,
                spender=spender,
            )
            if allowance != intent.request.input_amount_raw:
                return await self._submit_leg(
                    intent,
                    plan=plan,
                    template=approval,
                    existing=intent.approval_transaction,
                    existing_state=intent.approval_transaction_state,
                    rpc=rpc,
                    next_state=OnchainExecutionState.APPROVAL_SUBMITTED,
                    now_ms=int(self._clock_ms()),
                )
        return await self._submit_leg(
            intent,
            plan=plan,
            template=plan.swap,
            existing=intent.swap_transaction,
            existing_state=intent.swap_transaction_state,
            rpc=rpc,
            next_state=OnchainExecutionState.SWAP_SUBMITTED,
            now_ms=int(self._clock_ms()),
        )

    async def _approval_submitted(
        self,
        intent: OnchainExecutionIntent,
        *,
        rpc: OnchainExecutionRpc,
        now_ms: int,
    ) -> str:
        signed = intent.approval_transaction
        if signed is None:
            raise RuntimeError("onchain_executor_approval_fence_missing")
        receipt = await rpc.receipt(signed.transaction_hash)
        if receipt is None:
            return "approval_pending"
        if not _receipt_succeeded(receipt):
            self._store.settle_signed(
                intent.execution_id,
                leg="approval",
                state="FAILED",
                receipt=dict(receipt),
                error_code="onchain_approval_reverted",
                now_ms=now_ms,
            )
            return self._fail(intent, "onchain_approval_reverted", now_ms=now_ms)
        if intent.approval_transaction_state == "SUBMITTED" and not self._store.settle_signed(
            intent.execution_id,
            leg="approval",
            state="CONFIRMED",
            receipt=dict(receipt),
            now_ms=now_ms,
        ):
            raise RuntimeError("onchain_executor_approval_settlement_conflict")
        if intent.plan is None:
            raise RuntimeError("onchain_executor_plan_missing")
        return await self._submit_leg(
            intent,
            plan=intent.plan,
            template=intent.plan.swap,
            existing=intent.swap_transaction,
            existing_state=intent.swap_transaction_state,
            rpc=rpc,
            next_state=OnchainExecutionState.SWAP_SUBMITTED,
            now_ms=now_ms,
        )

    async def _swap_submitted(
        self,
        intent: OnchainExecutionIntent,
        *,
        rpc: OnchainExecutionRpc,
        now_ms: int,
    ) -> str:
        signed = intent.swap_transaction
        if signed is None:
            raise RuntimeError("onchain_executor_swap_fence_missing")
        receipt = await rpc.receipt(signed.transaction_hash)
        if receipt is None:
            return "swap_pending"
        succeeded = _receipt_succeeded(receipt)
        if intent.swap_transaction_state == "SUBMITTED" and not self._store.settle_signed(
            intent.execution_id,
            leg="swap",
            state="CONFIRMED" if succeeded else "FAILED",
            receipt=dict(receipt),
            error_code=None if succeeded else "onchain_swap_reverted",
            now_ms=now_ms,
        ):
            raise RuntimeError("onchain_executor_swap_settlement_conflict")
        target = OnchainExecutionState.CONFIRMED if succeeded else OnchainExecutionState.FAILED
        code = None if succeeded else "onchain_swap_reverted"
        if not self._store.advance(
            intent.execution_id,
            expected_state=OnchainExecutionState.SWAP_SUBMITTED,
            state=target,
            error_code=code,
            now_ms=now_ms,
        ):
            raise RuntimeError("onchain_executor_swap_state_conflict")
        return "swap_confirmed" if succeeded else "swap_failed"

    async def _submit_leg(
        self,
        intent: OnchainExecutionIntent,
        *,
        plan: OnchainExecutionPlan,
        template: OnchainTransactionTemplate,
        existing: OnchainSignedTransaction | None,
        existing_state: str | None,
        rpc: OnchainExecutionRpc,
        next_state: OnchainExecutionState,
        now_ms: int,
    ) -> str:
        signed = existing
        if signed is None:
            try:
                validate_onchain_execution_plan(intent, plan, now_ms=int(self._clock_ms()))
            except ValueError as exc:
                return self._fail(intent, str(exc), now_ms=int(self._clock_ms()))
            await rpc.simulate(template)
            nonce = await rpc.pending_nonce(intent.wallet_address)
            gas_limit = await rpc.estimate_gas(template)
            gas_price = await rpc.gas_price()
            signed = self._signer.sign(
                template,
                nonce=nonce,
                gas_limit=gas_limit,
                gas_price=gas_price,
            )
            if not self._store.append_signed(intent.execution_id, signed=signed, now_ms=now_ms):
                raise RuntimeError("onchain_executor_signed_fence_conflict")
            existing_state = "SIGNED"
        if existing_state == "SIGNED":
            receipt = await rpc.receipt(signed.transaction_hash)
            if receipt is None:
                await rpc.send_raw_transaction(signed)
            if not self._store.settle_signed(
                intent.execution_id,
                leg=signed.leg,
                state="SUBMITTED",
                now_ms=int(self._clock_ms()),
            ):
                raise RuntimeError("onchain_executor_submission_settlement_conflict")
        if not self._store.advance(
            intent.execution_id,
            expected_state=intent.state,
            state=next_state,
            now_ms=int(self._clock_ms()),
        ):
            raise RuntimeError("onchain_executor_state_fence_conflict")
        return f"{signed.leg}_submitted"

    def _fail(self, intent: OnchainExecutionIntent, error_code: str, *, now_ms: int) -> str:
        normalized = str(error_code).strip()[:100] or "onchain_execution_failed"
        if not self._store.advance(
            intent.execution_id,
            expected_state=intent.state,
            state=OnchainExecutionState.FAILED,
            error_code=normalized,
            now_ms=now_ms,
        ):
            raise RuntimeError("onchain_executor_failure_settlement_conflict")
        return "failed"


def _receipt_succeeded(receipt: Mapping[str, Any]) -> bool:
    status = receipt.get("status")
    if not isinstance(status, str) or status not in {"0x0", "0x1"}:
        raise RuntimeError("onchain_rpc_receipt_status_invalid")
    return status == "0x1"


__all__ = ["OnchainExecutionService"]
