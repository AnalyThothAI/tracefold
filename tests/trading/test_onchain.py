from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from tracefold.trading.onchain import (
    OnchainAssetCandidate,
    OnchainProviderToken,
    OnchainRouteQuote,
    RouteAnalysisState,
    analyze_onchain_routes,
    resolve_onchain_candidates,
)

NOW = 1_900_000_000_000
USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
HYPE_ETH = "0x1111111111111111111111111111111111111111"
HYPE_BASE = "0x2222222222222222222222222222222222222222"


def test_onchain_identity_is_chain_and_canonical_contract_not_ticker() -> None:
    ethereum = OnchainAssetCandidate(
        chain_id=1,
        chain_name="Ethereum",
        contract_address=HYPE_ETH.upper().replace("0X", "0x"),
        symbol="hype",
        name="Hyperliquid",
        decimals=18,
        providers=("okx",),
        verified=True,
        confidence_bps=9_000,
    )
    base = ethereum.model_copy(update={"chain_id": 8453, "chain_name": "Base", "contract_address": HYPE_BASE})

    assert ethereum.symbol == "HYPE"
    assert ethereum.contract_address == HYPE_ETH
    assert ethereum.identity != base.identity


def test_non_evm_or_native_alias_cannot_become_a_contract_identity() -> None:
    with pytest.raises(ValidationError):
        OnchainAssetCandidate(
            chain_id=1,
            chain_name="Ethereum",
            contract_address="native",
            symbol="ETH",
            name="Ether",
            decimals=18,
            providers=("okx",),
            verified=True,
            confidence_bps=9_000,
        )


def test_resolver_merges_provider_evidence_but_keeps_ambiguous_contracts_separate() -> None:
    candidates = resolve_onchain_candidates(
        "HYPE",
        (
            OnchainProviderToken(
                provider="okx",
                chain_id=1,
                chain_name="Ethereum",
                contract_address=HYPE_ETH,
                symbol="HYPE",
                name="Hyperliquid",
                decimals=18,
                verified=True,
            ),
            OnchainProviderToken(
                provider="oneinch",
                chain_id=1,
                chain_name="Ethereum",
                contract_address=HYPE_ETH,
                symbol="HYPE",
                name="Hyperliquid",
                decimals=18,
                verified=True,
            ),
            OnchainProviderToken(
                provider="oneinch",
                chain_id=8453,
                chain_name="Base",
                contract_address=HYPE_BASE,
                symbol="HYPE",
                name="Hype Token",
                decimals=18,
                verified=False,
            ),
            OnchainProviderToken(
                provider="okx",
                chain_id=1,
                chain_name="Ethereum",
                contract_address="0x3333333333333333333333333333333333333333",
                symbol="NOTHYPE",
                name="Headline text must not match",
                decimals=18,
                verified=True,
            ),
        ),
    )

    assert [(value.chain_id, value.contract_address) for value in candidates] == [
        (1, HYPE_ETH),
        (8453, HYPE_BASE),
    ]
    assert candidates[0].providers == ("okx", "oneinch")
    assert candidates[0].confidence_bps > candidates[1].confidence_bps


def _quote(
    provider: str,
    output_raw: int,
    *,
    output_usd: Decimal | None,
    fee_usd: Decimal | None,
    gas_usd: Decimal | None,
    simulation_passed: bool | None,
    risk_checked: bool,
    received_at_ms: int = NOW,
    expires_at_ms: int = NOW + 20_000,
) -> OnchainRouteQuote:
    return OnchainRouteQuote(
        provider=provider,
        chain_id=1,
        input_contract=USDC,
        output_contract=HYPE_ETH,
        input_amount_raw=10_000_000,
        expected_output_raw=output_raw,
        minimum_output_raw=output_raw * 99 // 100,
        expected_output_usd=output_usd,
        provider_fee_usd=fee_usd,
        gas_fee_usd=gas_usd,
        price_impact_bps=12,
        slippage_bps=100,
        route_labels=("Uniswap V3",),
        latency_ms=210,
        received_at_ms=received_at_ms,
        expires_at_ms=expires_at_ms,
        simulation_passed=simulation_passed,
        risk_checked=risk_checked,
        risk_blocked=False,
    )


def test_route_analysis_uses_net_receive_when_every_safety_and_cost_fact_is_complete() -> None:
    analysis = analyze_onchain_routes(
        (
            _quote(
                "okx",
                1_000_000_000_000_000_000,
                output_usd=Decimal("10.10"),
                fee_usd=Decimal("0.02"),
                gas_usd=Decimal("0.13"),
                simulation_passed=True,
                risk_checked=True,
            ),
            _quote(
                "oneinch",
                995_000_000_000_000_000,
                output_usd=Decimal("10.08"),
                fee_usd=Decimal("0"),
                gas_usd=Decimal("0.04"),
                simulation_passed=True,
                risk_checked=True,
            ),
        ),
        now_ms=NOW,
    )

    assert analysis.state is RouteAnalysisState.DEFINITIVE
    assert analysis.winner_provider == "oneinch"
    assert analysis.winner_net_receive_usd == Decimal("10.04")
    assert analysis.reason_codes == ()


def test_route_analysis_is_provisional_instead_of_faking_complete_costs() -> None:
    analysis = analyze_onchain_routes(
        (
            _quote(
                "okx",
                1_000_000_000_000_000_000,
                output_usd=None,
                fee_usd=Decimal("0.01"),
                gas_usd=None,
                simulation_passed=None,
                risk_checked=False,
            ),
            _quote(
                "oneinch",
                990_000_000_000_000_000,
                output_usd=None,
                fee_usd=None,
                gas_usd=None,
                simulation_passed=None,
                risk_checked=False,
            ),
        ),
        now_ms=NOW,
    )

    assert analysis.state is RouteAnalysisState.PROVISIONAL
    assert analysis.winner_provider == "okx"
    assert set(analysis.reason_codes) == {"cost_incomplete", "simulation_incomplete", "risk_check_incomplete"}


def test_definitive_ranking_treats_zero_net_receive_as_a_real_value() -> None:
    analysis = analyze_onchain_routes(
        (
            _quote(
                "okx",
                1_000,
                output_usd=Decimal("1"),
                fee_usd=Decimal("0.5"),
                gas_usd=Decimal("0.5"),
                simulation_passed=True,
                risk_checked=True,
            ),
            _quote(
                "oneinch",
                2_000,
                output_usd=Decimal("1"),
                fee_usd=Decimal("1"),
                gas_usd=Decimal("1"),
                simulation_passed=True,
                risk_checked=True,
            ),
        ),
        now_ms=NOW,
    )

    assert analysis.winner_provider == "okx"
    assert analysis.winner_net_receive_usd == Decimal("0")


def test_route_analysis_hard_rejects_expired_simulation_failed_and_risk_blocked_routes() -> None:
    expired = _quote(
        "okx",
        1_000,
        output_usd=Decimal("10"),
        fee_usd=Decimal("0"),
        gas_usd=Decimal("0"),
        simulation_passed=True,
        risk_checked=True,
        received_at_ms=NOW - 2_000,
        expires_at_ms=NOW - 1,
    )
    failed = _quote(
        "oneinch",
        2_000,
        output_usd=Decimal("20"),
        fee_usd=Decimal("0"),
        gas_usd=Decimal("0"),
        simulation_passed=False,
        risk_checked=True,
    )
    blocked = _quote(
        "binance",
        3_000,
        output_usd=Decimal("30"),
        fee_usd=Decimal("0"),
        gas_usd=Decimal("0"),
        simulation_passed=True,
        risk_checked=True,
    ).model_copy(update={"risk_blocked": True})

    analysis = analyze_onchain_routes((expired, failed, blocked), now_ms=NOW)

    assert analysis.state is RouteAnalysisState.UNAVAILABLE
    assert analysis.winner_provider is None
    assert set(analysis.rejected_providers) == {"okx", "oneinch", "binance"}
