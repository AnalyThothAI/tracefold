"""The venue read the Runtime makes itself: signed positionRisk, and unknown whenever it fails (#680 PR-3)."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from nautilus_trader.adapters.binance.http.error import BinanceClientError

from tracefold.integrations.nautilus.oi_runtime.binance import BinanceVenuePositions
from tracefold.integrations.nautilus.oi_runtime.config import BinanceRuntimeCredentials
from tracefold.integrations.nautilus.oi_runtime.venue import VenueReading, read_failure, watch_venue

_CREDENTIALS = BinanceRuntimeCredentials("paper-key", "paper-secret")


class _Account:
    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)
        self.recv_windows: list[str | None] = []

    async def query_futures_position_risk(self, symbol: str | None = None, recv_window: str | None = None) -> Any:
        self.recv_windows.append(recv_window)
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


def _row(symbol: str, amount: str, side: str = "BOTH") -> Any:
    return SimpleNamespace(symbol=symbol, positionAmt=amount, positionSide=side)


_RECV_WINDOW = BinanceClientError(
    status=400, message={"code": -1021, "msg": "Timestamp for this request is outside of the recvWindow."}, headers={}
)


def test_position_risk_is_read_as_signed_positions_and_flat_rows_are_not_positions() -> None:
    account = _Account([_row("APTUSDT", "1188.3"), _row("ETHUSDT", "-0.050"), _row("BTCUSDT", "0.000")])
    venue = BinanceVenuePositions(environment=None, credentials=_CREDENTIALS, account=account)

    positions = asyncio.run(venue.read())

    assert positions == {"APTUSDT": Decimal("1188.3"), "ETHUSDT": Decimal("-0.050")}
    # The venue's widest recvWindow: a read has no side effect a late arrival could repeat.
    assert account.recv_windows == ["60000"]


def test_a_position_risk_error_is_raised_never_answered_as_flat() -> None:
    venue = BinanceVenuePositions(environment=None, credentials=_CREDENTIALS, account=_Account(_RECV_WINDOW))

    with pytest.raises(BinanceClientError):
        asyncio.run(venue.read())


def test_the_reader_hands_every_outcome_to_the_strategy_and_a_failure_is_unknown() -> None:
    account = _Account([_row("APTUSDT", "1188.3")], _RECV_WINDOW, [])
    venue = BinanceVenuePositions(environment=None, credentials=_CREDENTIALS, account=account)
    seen: list[VenueReading] = []
    clock = iter(range(1_000, 10_000, 10))

    async def run() -> None:
        stop = asyncio.Event()

        def observe(reading: VenueReading) -> None:
            seen.append(reading)
            if len(seen) == 3:
                stop.set()

        await asyncio.wait_for(
            watch_venue(venue.read, observe, stop, interval_seconds=0.0, clock_ns=lambda: next(clock)), timeout=5.0
        )

    asyncio.run(run())

    assert [(reading.known, reading.failure) for reading in seen] == [
        (True, None),
        (False, "BinanceClientError:-1021"),
        (True, None),
    ]
    assert seen[0].positions == {"APTUSDT": Decimal("1188.3")}
    assert seen[1].positions is None and seen[1].quantity("APTUSDT") == 0
    assert seen[2].positions == {}
    assert all(reading.completed_at_ns >= reading.started_at_ns for reading in seen)


def test_a_read_that_does_not_answer_in_time_is_unknown() -> None:
    seen: list[VenueReading] = []

    async def never() -> dict[str, Decimal]:
        await asyncio.sleep(10)
        return {}

    async def run() -> None:
        stop = asyncio.Event()

        def observe(reading: VenueReading) -> None:
            seen.append(reading)
            stop.set()

        await watch_venue(never, observe, stop, interval_seconds=0.0, timeout_seconds=0.01)

    asyncio.run(run())

    [reading] = seen
    assert (reading.known, reading.failure) == (False, "timeout")


def test_a_reading_is_either_known_or_failed_and_names_no_secret() -> None:
    with pytest.raises(ValueError, match="oi_runtime_venue_reading_invalid"):
        VenueReading(1, 2, None)
    with pytest.raises(ValueError, match="oi_runtime_venue_reading_invalid"):
        VenueReading(1, 2, {}, failure="timeout")
    with pytest.raises(ValueError, match="oi_runtime_venue_reading_clock_invalid"):
        VenueReading(2, 1, {})
    assert read_failure(ConnectionResetError("tls close_notify from key=abc")) == "ConnectionResetError"
