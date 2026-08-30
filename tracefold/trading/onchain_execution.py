"""Provider-neutral EVM execution values for the manual onchain wallet lane."""

from __future__ import annotations

import hashlib
import re
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Annotated, Literal

from eth_abi.abi import decode as decode_abi
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .onchain import MAX_DEVELOPMENT_TEST_ONCHAIN_NOTIONAL_USD, OnchainProvider, OnchainQuoteRequest, OnchainRouteQuote

_ADDRESS_RE = re.compile(r"0x[0-9a-f]{40}")
_HEX_DATA_RE = re.compile(r"0x(?:[0-9a-f]{2})*")
_TX_HASH_RE = re.compile(r"0x[0-9a-f]{64}")
_ONEINCH_ROUTER_V6 = "0x111111125421ca6dc452d289314280a0f8842a65"
_ONEINCH_SWAP_SELECTOR = "0x07ed2379"
_OKX_ROUTER_BY_CHAIN = {
    1: "0x8feab81d36e7576107d5de0758c1b839be31b4f6",
    56: "0x5994814f2c4040b863a0125a45de152a8c2a4dec",
    8453: "0x67d03631fe51b741c0c00c4e16eb662ac84381df",
    42161: "0x09f94b5fc68e227c323a6fbae3bd98c97fd8c849",
    4663: "0x6e2a35a7ad683cf634d91492d73bb7ff774c6919",
}
_OKX_APPROVAL_PROXY_BY_CHAIN = {
    1: "0x40aa958dd87fc8305b97f2ba922cddca374bcd7f",
    56: "0x2c34a2fb1d0b4f55de51e1d0bdefaddce6b7cdd6",
    8453: "0x57df6092665eb6058de53939612413ff4b09114e",
    42161: "0x70cbb871e8f30fc8ce23609e9e0ea87b6b222f58",
    4663: "0x42170295f1173c9e5874ea9d00c6d137e1a4f53d",
}
# Selector -> (BaseRequest first word, optional receiver word, minimum ABI head words).
# These are the four exact-in entry points published by OKX's Web3-DEX-Router-EVM-V1 contract.
_OKX_EXACT_IN_SELECTORS: dict[str, tuple[int, int | None, int]] = {
    "0xb80c2f09": (1, None, 9),  # smartSwapByOrderId
    "0x03b87e5f": (2, 1, 10),  # smartSwapTo
    "0xf2c42696": (1, None, 7),  # dagSwapByOrderId
    "0x0c307f76": (2, 1, 8),  # dagSwapTo
}
_EVM_ADDRESS_MASK = (1 << 160) - 1


def canonical_evm_address(value: object) -> str:
    normalized = str(value).strip().lower()
    if _ADDRESS_RE.fullmatch(normalized) is None:
        raise ValueError("onchain_evm_address_invalid")
    return normalized


def onchain_wallet_fingerprint(address: str) -> str:
    canonical = canonical_evm_address(address)
    return hashlib.sha256(f"tracefold:onchain:manual:evm:{canonical}".encode()).hexdigest()


class OnchainTransactionTemplate(BaseModel):
    """One provider-built EVM call before nonce assignment and local signing."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    template_version: Literal["onchain_transaction_template_v1"] = "onchain_transaction_template_v1"
    provider: OnchainProvider
    leg: Literal["approval", "swap"]
    chain_id: Annotated[int, Field(gt=0)]
    from_address: str
    to_address: str
    data: str
    value: Annotated[int, Field(ge=0)] = 0
    gas_limit: Annotated[int, Field(gt=0)] | None = None
    gas_price: Annotated[int, Field(gt=0)] | None = None

    _normalize_from = field_validator("from_address", mode="before")(canonical_evm_address)
    _normalize_to = field_validator("to_address", mode="before")(canonical_evm_address)

    @field_validator("data", mode="before")
    @classmethod
    def normalize_data(cls, value: object) -> str:
        normalized = str(value).strip().lower()
        if _HEX_DATA_RE.fullmatch(normalized) is None:
            raise ValueError("onchain_transaction_data_invalid")
        return normalized


class OnchainExecutionPlan(BaseModel):
    """Exact route and calls authorized by one Telegram confirmation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    plan_version: Literal["onchain_execution_plan_v1"] = "onchain_execution_plan_v1"
    provider: OnchainProvider
    wallet_address: str
    wallet_fingerprint: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    request: OnchainQuoteRequest
    quote: OnchainRouteQuote
    approval: OnchainTransactionTemplate | None
    swap: OnchainTransactionTemplate
    prepared_at_ms: Annotated[int, Field(gt=0)]
    expires_at_ms: Annotated[int, Field(gt=0)]

    _normalize_wallet = field_validator("wallet_address", mode="before")(canonical_evm_address)

    @model_validator(mode="after")
    def validate_identity(self) -> OnchainExecutionPlan:
        expected = (
            self.request.chain_id,
            self.request.input_contract,
            self.request.output_contract,
            self.request.input_amount_raw,
        )
        observed = (
            self.quote.chain_id,
            self.quote.input_contract,
            self.quote.output_contract,
            self.quote.input_amount_raw,
        )
        if observed != expected or self.quote.provider != self.provider:
            raise ValueError("onchain_execution_quote_identity_mismatch")
        calls = (self.swap,) if self.approval is None else (self.approval, self.swap)
        if any(
            call.provider != self.provider
            or call.chain_id != self.request.chain_id
            or call.from_address != self.wallet_address
            for call in calls
        ):
            raise ValueError("onchain_execution_transaction_identity_mismatch")
        if self.swap.leg != "swap" or (self.approval is not None and self.approval.leg != "approval"):
            raise ValueError("onchain_execution_transaction_leg_invalid")
        if self.approval is None:
            raise ValueError("onchain_execution_approval_missing")
        approval_spender, approval_amount = decode_erc20_approval(self.approval.data)
        if (
            self.approval.to_address != self.request.input_contract
            or self.approval.value != 0
            or approval_amount != self.request.input_amount_raw
            or self.swap.value != 0
        ):
            raise ValueError("onchain_execution_transaction_authority_invalid")
        if self.provider == "oneinch":
            if approval_spender != self.swap.to_address:
                raise ValueError("onchain_execution_transaction_authority_invalid")
            _validate_oneinch_swap(self)
        elif self.provider == "okx":
            _validate_okx_swap(self, approval_spender=approval_spender)
        else:
            raise ValueError("onchain_execution_provider_calldata_verifier_unavailable")
        if self.wallet_fingerprint != onchain_wallet_fingerprint(self.wallet_address):
            raise ValueError("onchain_execution_wallet_fingerprint_mismatch")
        if self.expires_at_ms <= self.prepared_at_ms:
            raise ValueError("onchain_execution_expiry_invalid")
        return self


class OnchainSignedTransaction(BaseModel):
    """A locally signed transaction whose exact bytes can be safely rebroadcast."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    signed_version: Literal["onchain_signed_transaction_v1"] = "onchain_signed_transaction_v1"
    provider: OnchainProvider
    leg: Literal["approval", "swap"]
    chain_id: Annotated[int, Field(gt=0)]
    wallet_address: str
    nonce: Annotated[int, Field(ge=0)] | None = None
    raw_transaction: str | None = None
    transaction_hash: str

    _normalize_wallet = field_validator("wallet_address", mode="before")(canonical_evm_address)

    @field_validator("raw_transaction", mode="before")
    @classmethod
    def normalize_raw_transaction(cls, value: object) -> str | None:
        if value is None:
            return None
        normalized = str(value).strip().lower()
        if _HEX_DATA_RE.fullmatch(normalized) is None or len(normalized) <= 4:
            raise ValueError("onchain_signed_transaction_invalid")
        return normalized

    @model_validator(mode="after")
    def validate_private_signing_material_shape(self) -> OnchainSignedTransaction:
        if (self.nonce is None) != (self.raw_transaction is None):
            raise ValueError("onchain_signed_transaction_private_shape_invalid")
        return self

    @field_validator("transaction_hash", mode="before")
    @classmethod
    def normalize_transaction_hash(cls, value: object) -> str:
        normalized = str(value).strip().lower()
        if _TX_HASH_RE.fullmatch(normalized) is None:
            raise ValueError("onchain_transaction_hash_invalid")
        return normalized


class OnchainExecutionState(StrEnum):
    AWAITING_CONFIRMATION = "AWAITING_CONFIRMATION"
    PENDING = "PENDING"
    CLAIMED = "CLAIMED"
    APPROVAL_SUBMITTED = "APPROVAL_SUBMITTED"
    SWAP_SUBMITTED = "SWAP_SUBMITTED"
    CONFIRMED = "CONFIRMED"
    FAILED = "FAILED"
    AMBIGUOUS = "AMBIGUOUS"
    CANCELLED = "CANCELLED"


class OnchainExecutionIntent(BaseModel):
    """Durable manual-wallet execution requested and confirmed from one Telegram session."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    execution_id: Annotated[str, Field(pattern=r"^[0-9a-f]{8}-[0-9a-f-]{27}$")]
    session_id: Annotated[str, Field(pattern=r"^[0-9a-f]{8}-[0-9a-f-]{27}$")]
    actor_user_id: Annotated[int, Field(gt=0)]
    chat_id: int
    interaction_message_id: Annotated[int, Field(gt=0)]
    provider: OnchainProvider
    wallet_address: str
    wallet_fingerprint: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    development_test: bool = False
    notional_usd: Decimal = Decimal("10")
    settlement_decimals: Annotated[int, Field(ge=0, le=255)] = 6
    request: OnchainQuoteRequest
    quote: OnchainRouteQuote
    state: OnchainExecutionState
    confirmation_update_id: Annotated[int, Field(ge=0)] | None = None
    plan: OnchainExecutionPlan | None = None
    approval_transaction: OnchainSignedTransaction | None = None
    approval_transaction_state: Literal["SIGNED", "SUBMITTED", "CONFIRMED", "FAILED", "AMBIGUOUS"] | None = None
    swap_transaction: OnchainSignedTransaction | None = None
    swap_transaction_state: Literal["SIGNED", "SUBMITTED", "CONFIRMED", "FAILED", "AMBIGUOUS"] | None = None
    error_code: Annotated[str, Field(min_length=1, max_length=100)] | None = None
    created_at_ms: Annotated[int, Field(gt=0)]
    confirmed_at_ms: Annotated[int, Field(gt=0)] | None = None
    updated_at_ms: Annotated[int, Field(gt=0)]

    _normalize_wallet = field_validator("wallet_address", mode="before")(canonical_evm_address)

    @field_validator("notional_usd", mode="before")
    @classmethod
    def validate_notional_usd(cls, value: object) -> Decimal:
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError("onchain_execution_notional_invalid") from exc
        if not parsed.is_finite() or parsed <= 0:
            raise ValueError("onchain_execution_notional_invalid")
        return parsed

    @model_validator(mode="after")
    def validate_execution_intent(self) -> OnchainExecutionIntent:
        if self.wallet_fingerprint != onchain_wallet_fingerprint(self.wallet_address):
            raise ValueError("onchain_execution_wallet_fingerprint_mismatch")
        if self.development_test and self.notional_usd > MAX_DEVELOPMENT_TEST_ONCHAIN_NOTIONAL_USD:
            raise ValueError("onchain_development_test_notional_exceeds_cap")
        expected_input_raw = int(self.notional_usd * (Decimal(10) ** self.settlement_decimals))
        if self.request.input_amount_raw != expected_input_raw:
            raise ValueError("onchain_execution_notional_amount_mismatch")
        if self.quote.provider != self.provider:
            raise ValueError("onchain_execution_provider_mismatch")
        if self.updated_at_ms < self.created_at_ms:
            raise ValueError("onchain_execution_time_invalid")
        confirmed = self.confirmation_update_id is not None and self.confirmed_at_ms is not None
        if (self.state is OnchainExecutionState.AWAITING_CONFIRMATION) == confirmed:
            raise ValueError("onchain_execution_confirmation_shape_invalid")
        if self.plan is not None and (
            self.plan.provider != self.provider
            or self.plan.wallet_address != self.wallet_address
            or self.plan.request != self.request
        ):
            raise ValueError("onchain_execution_plan_mismatch")
        if self.approval_transaction is not None and self.approval_transaction.leg != "approval":
            raise ValueError("onchain_execution_approval_leg_invalid")
        if (self.approval_transaction is None) != (self.approval_transaction_state is None):
            raise ValueError("onchain_execution_approval_state_invalid")
        if self.swap_transaction is not None and self.swap_transaction.leg != "swap":
            raise ValueError("onchain_execution_swap_leg_invalid")
        if (self.swap_transaction is None) != (self.swap_transaction_state is None):
            raise ValueError("onchain_execution_swap_state_invalid")
        if self.state in {OnchainExecutionState.FAILED, OnchainExecutionState.AMBIGUOUS}:
            if self.error_code is None:
                raise ValueError("onchain_execution_error_missing")
        elif self.error_code is not None:
            raise ValueError("onchain_execution_error_unexpected")
        return self


def validate_onchain_execution_plan(
    intent: OnchainExecutionIntent,
    plan: OnchainExecutionPlan,
    *,
    now_ms: int,
) -> None:
    """Fail closed when the executable Q2 route no longer satisfies the confirmed quote floor."""

    if now_ms > plan.expires_at_ms:
        raise ValueError("onchain_execution_plan_expired")
    if (
        plan.provider != intent.provider
        or plan.wallet_address != intent.wallet_address
        or plan.wallet_fingerprint != intent.wallet_fingerprint
        or plan.request != intent.request
    ):
        raise ValueError("onchain_execution_plan_identity_mismatch")
    confirmed_floor = intent.quote.minimum_output_raw
    executable_floor = plan.quote.minimum_output_raw
    if confirmed_floor is None or executable_floor is None or executable_floor < confirmed_floor:
        raise ValueError("onchain_execution_quote_drift_exceeded")


def decode_erc20_approval(data: str) -> tuple[str, int]:
    """Return the spender and exact amount from approve(address,uint256) calldata."""

    normalized = str(data).strip().lower()
    if not normalized.startswith("0x095ea7b3") or len(normalized) != 138:
        raise ValueError("onchain_approval_calldata_invalid")
    spender = canonical_evm_address("0x" + normalized[34:74])
    amount = int(normalized[74:138], 16)
    if amount <= 0:
        raise ValueError("onchain_approval_amount_invalid")
    return spender, amount


def _validate_oneinch_swap(plan: OnchainExecutionPlan) -> None:
    """Bind supported 1inch Router V6 calldata to the confirmed exact-in trade."""

    swap = plan.swap
    if swap.to_address != _ONEINCH_ROUTER_V6 or not swap.data.startswith(_ONEINCH_SWAP_SELECTOR):
        raise ValueError("onchain_oneinch_router_invalid")
    try:
        _executor, description, _route_data = decode_abi(
            ["address", "(address,address,address,address,uint256,uint256,uint256)", "bytes"],
            bytes.fromhex(swap.data[10:]),
        )
        source, destination, _source_receiver, destination_receiver, amount, minimum, flags = description
        source_address = canonical_evm_address(source)
        destination_address = canonical_evm_address(destination)
        receiver_address = canonical_evm_address(destination_receiver)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("onchain_oneinch_calldata_invalid") from exc
    if (
        source_address != plan.request.input_contract
        or destination_address != plan.request.output_contract
        or receiver_address != plan.wallet_address
        or int(amount) != plan.request.input_amount_raw
        or int(minimum) < int(plan.quote.minimum_output_raw or 0)
        or int(minimum) <= 0
        or int(flags) & 1
    ):
        raise ValueError("onchain_oneinch_calldata_authority_invalid")


def okx_router_address(chain_id: int) -> str:
    try:
        return _OKX_ROUTER_BY_CHAIN[int(chain_id)]
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("onchain_okx_chain_unsupported") from exc


def okx_approval_proxy_address(chain_id: int) -> str:
    try:
        return _OKX_APPROVAL_PROXY_BY_CHAIN[int(chain_id)]
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("onchain_okx_chain_unsupported") from exc


def _validate_okx_swap(plan: OnchainExecutionPlan, *, approval_spender: str) -> None:
    """Bind OKX Router V1 calldata to the exact operator-confirmed trade authority."""

    expected_router = okx_router_address(plan.request.chain_id)
    if plan.swap.to_address != expected_router:
        raise ValueError("onchain_okx_router_invalid")
    if approval_spender != okx_approval_proxy_address(plan.request.chain_id):
        raise ValueError("onchain_okx_approval_proxy_invalid")
    selector = plan.swap.data[:10]
    layout = _OKX_EXACT_IN_SELECTORS.get(selector)
    if layout is None:
        raise ValueError("onchain_okx_router_invalid")
    base_word, receiver_word, minimum_head_words = layout
    try:
        payload = bytes.fromhex(plan.swap.data[10:])
        if len(payload) < minimum_head_words * 32:
            raise ValueError("head_too_short")
        source_raw = _okx_calldata_word(payload, base_word)
        source = _okx_uint_address(source_raw)
        destination = _okx_word_address(payload, base_word + 1)
        amount = _okx_calldata_word(payload, base_word + 2)
        minimum = _okx_calldata_word(payload, base_word + 3)
        deadline = _okx_calldata_word(payload, base_word + 4)
        receiver = plan.wallet_address if receiver_word is None else _okx_word_address(payload, receiver_word)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("onchain_okx_calldata_invalid") from exc
    quote_floor = int(plan.quote.minimum_output_raw or 0)
    if (
        source_raw >> 160
        or source != plan.request.input_contract
        or destination != plan.request.output_contract
        or receiver != plan.wallet_address
        or amount != plan.request.input_amount_raw
        or minimum < quote_floor
        or minimum <= 0
        or deadline < plan.prepared_at_ms // 1_000
    ):
        raise ValueError("onchain_okx_calldata_authority_invalid")


def _okx_calldata_word(payload: bytes, index: int) -> int:
    start = int(index) * 32
    word = payload[start : start + 32]
    if len(word) != 32:
        raise ValueError("onchain_okx_calldata_word_invalid")
    return int.from_bytes(word, "big")


def _okx_uint_address(value: int) -> str:
    return canonical_evm_address(f"0x{value & _EVM_ADDRESS_MASK:040x}")


def _okx_word_address(payload: bytes, index: int) -> str:
    value = _okx_calldata_word(payload, index)
    if value >> 160:
        raise ValueError("onchain_okx_calldata_address_invalid")
    return _okx_uint_address(value)


__all__ = [
    "OnchainExecutionIntent",
    "OnchainExecutionPlan",
    "OnchainExecutionState",
    "OnchainSignedTransaction",
    "OnchainTransactionTemplate",
    "canonical_evm_address",
    "decode_erc20_approval",
    "okx_approval_proxy_address",
    "okx_router_address",
    "onchain_wallet_fingerprint",
    "validate_onchain_execution_plan",
]
