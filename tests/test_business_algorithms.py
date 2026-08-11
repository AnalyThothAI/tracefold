from __future__ import annotations

from tracefold.market import (
    canonical_chain_address,
    canonical_chain_id,
    chain_address_key,
    market_tick_id,
    normalize_ca,
)


def test_market_identity_normalizes_evm_without_corrupting_solana_case() -> None:
    assert canonical_chain_id("Ethereum") == "eip155:1"
    assert canonical_chain_address("ethereum", "0xAbCd") == "0xabcd"
    assert chain_address_key("SOL", "AbCd") == ("solana", "AbCd")
    assert chain_address_key("SOL", "AbCd") != chain_address_key("solana", "abcd")


def test_market_identity_treats_robinhood_as_one_canonical_evm_chain() -> None:
    assert canonical_chain_id("robinhood") == "robinhood"
    assert normalize_ca(
        "0x6982508145454ce325ddbe47a25d4ec3d2311933",
        chain="robinhood",
    ) == ("robinhood", "0x6982508145454Ce325dDbE47a25d4ec3d2311933")


def test_market_tick_identity_is_stable_and_source_specific() -> None:
    first = market_tick_id(
        target_type="Asset",
        target_id="asset:solana:token:abc",
        source_provider="gmgn",
        observed_at_ms=1_778_145_100_000,
    )
    replay = market_tick_id(
        target_type="Asset",
        target_id="asset:solana:token:abc",
        source_provider="gmgn",
        observed_at_ms=1_778_145_100_000,
    )
    other_source = market_tick_id(
        target_type="Asset",
        target_id="asset:solana:token:abc",
        source_provider="binance",
        observed_at_ms=1_778_145_100_000,
    )

    assert first == replay
    assert first.startswith("market_tick:")
    assert first != other_source
