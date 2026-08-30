from __future__ import annotations

from decimal import Decimal

import pytest
from eth_abi.abi import encode as encode_abi

from tracefold.integrations.onchain import EvmPrivateKeySigner
from tracefold.trading import (
    OnchainExecutionIntent,
    OnchainExecutionPlan,
    OnchainExecutionState,
    OnchainQuoteRequest,
    OnchainRouteQuote,
    OnchainTransactionTemplate,
    onchain_wallet_fingerprint,
    validate_onchain_execution_plan,
)

NOW = 1_900_000_000_000
WALLET = "0x7e5f4552091a69125d5dfcb7b8c2659029395bdf"
USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
TOKEN = "0x1111111111111111111111111111111111111111"
ROUTER = "0x111111125421ca6dc452d289314280a0f8842a65"
EXECUTOR = "0x2222222222222222222222222222222222222222"
OKX_ROUTER = "0x8feab81d36e7576107d5de0758c1b839be31b4f6"
OKX_APPROVAL_PROXY = "0x40aa958dd87fc8305b97f2ba922cddca374bcd7f"


def _request() -> OnchainQuoteRequest:
    return OnchainQuoteRequest(
        chain_id=1,
        input_contract=USDC,
        output_contract=TOKEN,
        input_amount_raw=10_000_000,
        slippage_bps=100,
    )


def _quote(provider: str, *, expected: int, minimum: int) -> OnchainRouteQuote:
    return OnchainRouteQuote(
        provider=provider,
        chain_id=1,
        input_contract=USDC,
        output_contract=TOKEN,
        input_amount_raw=10_000_000,
        expected_output_raw=expected,
        minimum_output_raw=minimum,
        slippage_bps=100,
        latency_ms=50,
        received_at_ms=NOW,
        expires_at_ms=NOW + 30_000,
    )


def _template(
    provider: str,
    *,
    minimum: int = 990,
    receiver: str = WALLET,
    router: str = ROUTER,
) -> OnchainTransactionTemplate:
    data = "0x1234"
    if provider == "oneinch":
        encoded = encode_abi(
            ["address", "(address,address,address,address,uint256,uint256,uint256)", "bytes"],
            [EXECUTOR, (USDC, TOKEN, ROUTER, receiver, 10_000_000, minimum, 0), b"\x01"],
        )
        data = "0x07ed2379" + encoded.hex()
    return OnchainTransactionTemplate(
        provider=provider,
        leg="swap",
        chain_id=1,
        from_address=WALLET,
        to_address=router,
        data=data,
    )


def _approval(provider: str) -> OnchainTransactionTemplate:
    spender = OKX_APPROVAL_PROXY if provider == "okx" else ROUTER
    calldata = "0x095ea7b3" + spender[2:].rjust(64, "0") + hex(10_000_000)[2:].rjust(64, "0")
    return OnchainTransactionTemplate(
        provider=provider,
        leg="approval",
        chain_id=1,
        from_address=WALLET,
        to_address=USDC,
        data=calldata,
    )


def test_one_private_key_signs_every_executable_route_from_the_same_wallet() -> None:
    signer = EvmPrivateKeySigner("0x" + "0" * 63 + "1")

    okx = signer.sign(_template("okx"), nonce=7, gas_limit=120_000, gas_price=20_000_000_000)
    oneinch = signer.sign(_template("oneinch"), nonce=8, gas_limit=120_000, gas_price=20_000_000_000)

    assert signer.address == WALLET
    assert okx.wallet_address == oneinch.wallet_address == WALLET
    assert okx.provider == "okx"
    assert oneinch.provider == "oneinch"
    assert okx.transaction_hash != oneinch.transaction_hash


def test_fresh_route_must_preserve_the_output_floor_confirmed_in_telegram() -> None:
    request = _request()
    fingerprint = onchain_wallet_fingerprint(WALLET)
    confirmed_quote = _quote("oneinch", expected=1_000, minimum=990)
    intent = OnchainExecutionIntent(
        execution_id="11111111-1111-1111-1111-111111111111",
        session_id="22222222-2222-2222-2222-222222222222",
        actor_user_id=1,
        chat_id=1,
        interaction_message_id=1,
        provider="oneinch",
        wallet_address=WALLET,
        wallet_fingerprint=fingerprint,
        request=request,
        quote=confirmed_quote,
        state=OnchainExecutionState.CLAIMED,
        confirmation_update_id=1,
        created_at_ms=NOW,
        confirmed_at_ms=NOW + 1,
        updated_at_ms=NOW + 1,
    )
    plan = OnchainExecutionPlan(
        provider="oneinch",
        wallet_address=WALLET,
        wallet_fingerprint=fingerprint,
        request=request,
        quote=_quote("oneinch", expected=995, minimum=985),
        approval=_approval("oneinch"),
        swap=_template("oneinch", minimum=985),
        prepared_at_ms=NOW + 2,
        expires_at_ms=NOW + 20_000,
    )

    with pytest.raises(ValueError, match="onchain_execution_quote_drift_exceeded"):
        validate_onchain_execution_plan(intent, plan, now_ms=NOW + 3)


def test_development_test_execution_intent_has_a_hard_200u_ceiling() -> None:
    request = _request().model_copy(update={"input_amount_raw": 200_000_000})
    quote = _quote("oneinch", expected=1_000, minimum=990).model_copy(update={"input_amount_raw": 200_000_000})
    values = {
        "execution_id": "11111111-1111-1111-1111-111111111112",
        "session_id": "22222222-2222-2222-2222-222222222223",
        "actor_user_id": 1,
        "chat_id": 1,
        "interaction_message_id": 1,
        "provider": "oneinch",
        "wallet_address": WALLET,
        "wallet_fingerprint": onchain_wallet_fingerprint(WALLET),
        "development_test": True,
        "notional_usd": Decimal("200"),
        "request": request,
        "quote": quote,
        "state": OnchainExecutionState.CLAIMED,
        "confirmation_update_id": 1,
        "created_at_ms": NOW,
        "confirmed_at_ms": NOW + 1,
        "updated_at_ms": NOW + 1,
    }
    assert OnchainExecutionIntent(**values).notional_usd == Decimal("200")

    values["notional_usd"] = Decimal("200.01")
    with pytest.raises(ValueError, match="onchain_development_test_notional_exceeds_cap"):
        OnchainExecutionIntent(**values)

    values.update(development_test=False, notional_usd=Decimal("11"))
    with pytest.raises(ValueError, match="onchain_execution_notional_amount_mismatch"):
        OnchainExecutionIntent(**values)


def test_approval_spender_must_be_the_exact_swap_target() -> None:
    approval = _approval("oneinch").model_copy(
        update={
            "data": "0x095ea7b3"
            + "3333333333333333333333333333333333333333".rjust(64, "0")
            + hex(10_000_000)[2:].rjust(64, "0")
        }
    )

    with pytest.raises(ValueError, match="onchain_execution_transaction_authority_invalid"):
        OnchainExecutionPlan(
            provider="oneinch",
            wallet_address=WALLET,
            wallet_fingerprint=onchain_wallet_fingerprint(WALLET),
            request=_request(),
            quote=_quote("oneinch", expected=1_000, minimum=990),
            approval=approval,
            swap=_template("oneinch"),
            prepared_at_ms=NOW + 2,
            expires_at_ms=NOW + 20_000,
        )


@pytest.mark.parametrize(
    ("swap", "error"),
    [
        (
            _template("oneinch", receiver="0x3333333333333333333333333333333333333333"),
            "onchain_oneinch_calldata_authority_invalid",
        ),
        (_template("oneinch", minimum=989), "onchain_oneinch_calldata_authority_invalid"),
        (
            _template("oneinch", router="0x3333333333333333333333333333333333333333"),
            "onchain_oneinch_router_invalid",
        ),
    ],
)
def test_oneinch_calldata_is_bound_to_recipient_floor_and_router(
    swap: OnchainTransactionTemplate,
    error: str,
) -> None:
    approval = _approval("oneinch")
    if swap.to_address != ROUTER:
        approval = approval.model_copy(
            update={"data": "0x095ea7b3" + swap.to_address[2:].rjust(64, "0") + hex(10_000_000)[2:].rjust(64, "0")}
        )
    with pytest.raises(ValueError, match=error):
        OnchainExecutionPlan(
            provider="oneinch",
            wallet_address=WALLET,
            wallet_fingerprint=onchain_wallet_fingerprint(WALLET),
            request=_request(),
            quote=_quote("oneinch", expected=1_000, minimum=990),
            approval=approval,
            swap=swap,
            prepared_at_ms=NOW + 2,
            expires_at_ms=NOW + 20_000,
        )


def _okx_swap(
    *,
    receiver: str = WALLET,
    source: str = USDC,
    destination: str = TOKEN,
    amount: int = 10_000_000,
    minimum: int = 990,
    deadline: int = NOW // 1_000 + 300,
    selector: str = "0x03b87e5f",
    router: str = OKX_ROUTER,
) -> OnchainTransactionTemplate:
    base_request = (int(source, 16), destination, amount, minimum, deadline)
    if selector == "0x03b87e5f":
        types = [
            "uint256",
            "address",
            "(uint256,address,uint256,uint256,uint256)",
            "uint256[]",
            "(address[],address[],uint256[],bytes[],uint256)[][]",
            "(uint256,address,address,address,uint256,uint256,uint256,uint256,bool,bytes)[]",
        ]
        values = [1, receiver, base_request, [], [], []]
    else:
        types = [
            "uint256",
            "(uint256,address,uint256,uint256,uint256)",
            "uint256[]",
            "(address[],address[],uint256[],bytes[],uint256)[][]",
            "(uint256,address,address,address,uint256,uint256,uint256,uint256,bool,bytes)[]",
        ]
        values = [1, base_request, [], [], []]
    return OnchainTransactionTemplate(
        provider="okx",
        leg="swap",
        chain_id=1,
        from_address=WALLET,
        to_address=router,
        data=selector + encode_abi(types, values).hex(),
    )


@pytest.mark.parametrize("selector", ["0x03b87e5f", "0xb80c2f09"])
def test_okx_calldata_is_bound_to_official_router_proxy_pair_amount_floor_and_wallet(
    selector: str,
) -> None:
    plan = OnchainExecutionPlan(
        provider="okx",
        wallet_address=WALLET,
        wallet_fingerprint=onchain_wallet_fingerprint(WALLET),
        request=_request(),
        quote=_quote("okx", expected=1_000, minimum=990),
        approval=_approval("okx"),
        swap=_okx_swap(selector=selector),
        prepared_at_ms=NOW,
        expires_at_ms=NOW + 20_000,
    )

    assert plan.provider == "okx"


@pytest.mark.parametrize(
    ("swap", "error"),
    [
        (_okx_swap(receiver="0x3333333333333333333333333333333333333333"), "onchain_okx_calldata_authority_invalid"),
        (_okx_swap(destination="0x3333333333333333333333333333333333333333"), "onchain_okx_calldata_authority_invalid"),
        (_okx_swap(amount=9_999_999), "onchain_okx_calldata_authority_invalid"),
        (_okx_swap(minimum=989), "onchain_okx_calldata_authority_invalid"),
        (_okx_swap(deadline=NOW // 1_000 - 1), "onchain_okx_calldata_authority_invalid"),
        (_okx_swap(router=ROUTER), "onchain_okx_router_invalid"),
        (_template("okx", router=OKX_ROUTER), "onchain_okx_router_invalid"),
    ],
)
def test_okx_calldata_rejects_any_authority_or_identity_drift(
    swap: OnchainTransactionTemplate,
    error: str,
) -> None:
    with pytest.raises(ValueError, match=error):
        OnchainExecutionPlan(
            provider="okx",
            wallet_address=WALLET,
            wallet_fingerprint=onchain_wallet_fingerprint(WALLET),
            request=_request(),
            quote=_quote("okx", expected=1_000, minimum=990),
            approval=_approval("okx"),
            swap=swap,
            prepared_at_ms=NOW,
            expires_at_ms=NOW + 20_000,
        )


def test_okx_approval_must_target_the_code_owned_proxy() -> None:
    approval = _approval("okx").model_copy(
        update={"data": "0x095ea7b3" + OKX_ROUTER[2:].rjust(64, "0") + hex(10_000_000)[2:].rjust(64, "0")}
    )
    with pytest.raises(ValueError, match="onchain_okx_approval_proxy_invalid"):
        OnchainExecutionPlan(
            provider="okx",
            wallet_address=WALLET,
            wallet_fingerprint=onchain_wallet_fingerprint(WALLET),
            request=_request(),
            quote=_quote("okx", expected=1_000, minimum=990),
            approval=approval,
            swap=_okx_swap(),
            prepared_at_ms=NOW,
            expires_at_ms=NOW + 20_000,
        )
