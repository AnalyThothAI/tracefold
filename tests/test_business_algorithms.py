from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from tracefold.macro import resolve_completed_session
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


def test_macro_completed_session_obeys_settle_delay_and_market_calendar() -> None:
    before_settle = _epoch_ms(datetime(2026, 7, 23, 16, 15, tzinfo=_NEW_YORK))
    after_settle = _epoch_ms(datetime(2026, 7, 23, 16, 30, tzinfo=_NEW_YORK))
    independence_day = _epoch_ms(datetime(2026, 7, 4, 18, 0, tzinfo=_NEW_YORK))

    assert resolve_completed_session(now_ms=before_settle, settle_delay_seconds=1_800) == date(2026, 7, 22)
    assert resolve_completed_session(now_ms=after_settle, settle_delay_seconds=1_800) == date(2026, 7, 23)
    assert resolve_completed_session(now_ms=independence_day, settle_delay_seconds=0) == date(2026, 7, 2)


def _epoch_ms(value: datetime) -> int:
    return int(value.astimezone(UTC).timestamp() * 1_000)
