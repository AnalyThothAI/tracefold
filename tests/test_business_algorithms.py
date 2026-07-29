from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from tracefold.macro import resolve_thesis_session
from tracefold.market import canonical_chain_address, canonical_chain_id, chain_address_key, market_tick_id

_NEW_YORK = ZoneInfo("America/New_York")


def test_market_identity_normalizes_evm_without_corrupting_solana_case() -> None:
    assert canonical_chain_id("Ethereum") == "eip155:1"
    assert canonical_chain_address("ethereum", "0xAbCd") == "0xabcd"
    assert chain_address_key("SOL", "AbCd") == ("solana", "AbCd")
    assert chain_address_key("SOL", "AbCd") != chain_address_key("solana", "abcd")


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


def test_macro_thesis_session_obeys_0850_cutoff_and_market_calendar() -> None:
    before_cutoff = _epoch_ms(datetime(2026, 7, 23, 8, 49, tzinfo=_NEW_YORK))
    after_cutoff = _epoch_ms(datetime(2026, 7, 23, 8, 50, tzinfo=_NEW_YORK))
    independence_day = _epoch_ms(datetime(2026, 7, 4, 18, 0, tzinfo=_NEW_YORK))

    assert resolve_thesis_session(now_ms=before_cutoff) == date(2026, 7, 22)
    assert resolve_thesis_session(now_ms=after_cutoff) == date(2026, 7, 23)
    assert resolve_thesis_session(now_ms=independence_day) == date(2026, 7, 2)


def _epoch_ms(value: datetime) -> int:
    return int(value.astimezone(UTC).timestamp() * 1_000)
