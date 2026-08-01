from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

MarketState = Literal["open", "closed", "maintenance", "unknown", "not_applicable"]

_NEW_YORK = ZoneInfo("America/New_York")
_EQUITY_OPEN = time(4, 0)
_EQUITY_CLOSE = time(20, 0)
_EQUITY_EARLY_CLOSE = time(17, 0)
_FUTURES_CLOSE = time(17, 0)
_FUTURES_REOPEN = time(18, 0)


@dataclass(frozen=True, slots=True)
class MarketClock:
    state: MarketState
    expected_at_ms: int
    next_open_ms: int | None


def market_clock(calendar_id: str | None, *, now_ms: int) -> MarketClock:
    if calendar_id == "continuous":
        return MarketClock(state="open", expected_at_ms=int(now_ms), next_open_ms=None)
    if calendar_id == "us_equity_extended":
        return _equity_clock(now_ms)
    if calendar_id == "us_futures":
        return _futures_clock(now_ms)
    return MarketClock(state="unknown", expected_at_ms=int(now_ms), next_open_ms=None)


def is_us_market_session(session_date: date) -> bool:
    return session_date.weekday() < 5 and session_date not in _us_market_holidays(session_date.year)


def _equity_clock(now_ms: int) -> MarketClock:
    instant = datetime.fromtimestamp(int(now_ms) / 1_000, tz=UTC).astimezone(_NEW_YORK)
    session_date = instant.date()
    if is_us_market_session(session_date):
        opened = datetime.combine(session_date, _EQUITY_OPEN, tzinfo=_NEW_YORK)
        closed = datetime.combine(session_date, _equity_close(session_date), tzinfo=_NEW_YORK)
        if instant < opened:
            return MarketClock(
                state="closed",
                expected_at_ms=_epoch_ms(_previous_equity_close(session_date)),
                next_open_ms=_epoch_ms(opened),
            )
        if instant < closed:
            return MarketClock(state="open", expected_at_ms=int(now_ms), next_open_ms=None)
        return MarketClock(
            state="closed",
            expected_at_ms=_epoch_ms(closed),
            next_open_ms=_epoch_ms(_next_equity_open(session_date)),
        )
    return MarketClock(
        state="closed",
        expected_at_ms=_epoch_ms(_previous_equity_close(session_date)),
        next_open_ms=_epoch_ms(_next_equity_open(session_date)),
    )


def _futures_clock(now_ms: int) -> MarketClock:
    instant = datetime.fromtimestamp(int(now_ms) / 1_000, tz=UTC).astimezone(_NEW_YORK)
    current_time = instant.timetz().replace(tzinfo=None)
    weekday = instant.weekday()
    if weekday == 5 or (weekday == 6 and current_time < _FUTURES_REOPEN):
        friday = instant.date() - timedelta(days=(weekday - 4) % 7)
        friday_close = datetime.combine(friday, _FUTURES_CLOSE, tzinfo=_NEW_YORK)
        sunday = friday + timedelta(days=2)
        return MarketClock(
            state="closed",
            expected_at_ms=_epoch_ms(friday_close),
            next_open_ms=_epoch_ms(datetime.combine(sunday, _FUTURES_REOPEN, tzinfo=_NEW_YORK)),
        )
    if weekday == 4 and current_time >= _FUTURES_CLOSE:
        friday_close = datetime.combine(instant.date(), _FUTURES_CLOSE, tzinfo=_NEW_YORK)
        sunday = instant.date() + timedelta(days=2)
        return MarketClock(
            state="closed",
            expected_at_ms=_epoch_ms(friday_close),
            next_open_ms=_epoch_ms(datetime.combine(sunday, _FUTURES_REOPEN, tzinfo=_NEW_YORK)),
        )
    if weekday < 5 and _FUTURES_CLOSE <= current_time < _FUTURES_REOPEN:
        maintenance_start = datetime.combine(instant.date(), _FUTURES_CLOSE, tzinfo=_NEW_YORK)
        reopen = datetime.combine(instant.date(), _FUTURES_REOPEN, tzinfo=_NEW_YORK)
        return MarketClock(
            state="maintenance",
            expected_at_ms=_epoch_ms(maintenance_start),
            next_open_ms=_epoch_ms(reopen),
        )
    return MarketClock(state="open", expected_at_ms=int(now_ms), next_open_ms=None)


def _previous_equity_close(before_date: date) -> datetime:
    candidate = before_date
    while True:
        candidate -= timedelta(days=1)
        if is_us_market_session(candidate):
            return datetime.combine(candidate, _equity_close(candidate), tzinfo=_NEW_YORK)


def _next_equity_open(after_date: date) -> datetime:
    candidate = after_date
    while True:
        candidate += timedelta(days=1)
        if is_us_market_session(candidate):
            return datetime.combine(candidate, _EQUITY_OPEN, tzinfo=_NEW_YORK)


def _equity_close(session_date: date) -> time:
    thanksgiving = _nth_weekday(session_date.year, 11, weekday=3, occurrence=4)
    early_close_days = {
        thanksgiving + timedelta(days=1),
        date(session_date.year, 7, 3),
        date(session_date.year, 12, 24),
    }
    return _EQUITY_EARLY_CLOSE if session_date in early_close_days else _EQUITY_CLOSE


def _us_market_holidays(year: int) -> set[date]:
    holidays = {
        _observed_fixed_holiday(date(year, 1, 1)),
        _nth_weekday(year, 1, weekday=0, occurrence=3),
        _nth_weekday(year, 2, weekday=0, occurrence=3),
        _easter_sunday(year) - timedelta(days=2),
        _last_weekday(year, 5, weekday=0),
        _observed_fixed_holiday(date(year, 7, 4)),
        _nth_weekday(year, 9, weekday=0, occurrence=1),
        _nth_weekday(year, 11, weekday=3, occurrence=4),
        _observed_fixed_holiday(date(year, 12, 25)),
    }
    if year >= 2022:
        holidays.add(_observed_fixed_holiday(date(year, 6, 19)))
    return holidays


def _observed_fixed_holiday(day: date) -> date:
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def _nth_weekday(year: int, month: int, *, weekday: int, occurrence: int) -> date:
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (occurrence - 1))


def _last_weekday(year: int, month: int, *, weekday: int) -> date:
    last = date(year, month, monthrange(year, month)[1])
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _easter_sunday(year: int) -> date:
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    ell = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ell) // 451
    month = (h + ell - 7 * m + 114) // 31
    day = (h + ell - 7 * m + 114) % 31 + 1
    return date(year, month, day)


def _epoch_ms(value: datetime) -> int:
    return int(value.astimezone(UTC).timestamp() * 1_000)


__all__ = ["MarketClock", "MarketState", "is_us_market_session", "market_clock"]
