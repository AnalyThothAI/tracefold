"""Market marks: capture t0/+5m/+30m/+4h price/OI for candidate Events with grounded CEX assets."""

from __future__ import annotations

from typing import Any

MARK_OFFSETS_MS = {"t0": 0, "5m": 5 * 60_000, "30m": 30 * 60_000, "4h": 4 * 3600_000}


def _pct(before: Any, after: Any) -> float | None:
    try:
        b, a = float(before), float(after)
    except (TypeError, ValueError):
        return None
    return None if b == 0 else round((a - b) / b * 100.0, 4)


def capture_due_marks(repos: Any, *, now_ms: int, limit: int = 100) -> int:
    """Record marks whose offset has elapsed. Idempotent by (event_id, mark, symbol)."""

    news = repos.news
    written = 0
    for mark, offset in MARK_OFFSETS_MS.items():
        for row in news.events_due_for_mark(mark=mark, offset_ms=offset, now_ms=now_ms, limit=limit):
            event_id = str(row["event_id"])
            opened = int(row["opened_at_ms"])
            for raw_symbol in row.get("grounded_assets") or []:
                symbol = str(raw_symbol).upper().replace("XYZ-", "")
                target_id = news.cex_tick_target(symbol)
                if target_id is None:
                    news.record_mark(
                        event_id=event_id,
                        mark=mark,
                        symbol=symbol,
                        market_type=None,
                        price=None,
                        open_interest=None,
                        price_change_pct=None,
                        oi_change_pct=None,
                        captured_at_ms=now_ms,
                    )
                    continue
                at_ms = opened + offset
                tick = news.cex_tick_at(
                    target_id=target_id, at_ms=at_ms, lookback_ms=30 * 60_000 if mark == "t0" else offset
                )
                base = None
                if mark != "t0":
                    base_row = repos.conn.execute(
                        "SELECT price, open_interest FROM news_event_market_marks"
                        " WHERE event_id = %s AND mark = 't0' AND symbol = %s",
                        (event_id, symbol),
                    ).fetchone()
                    base = dict(base_row) if base_row else None
                price = float(tick["price_usd"]) if tick and tick.get("price_usd") is not None else None
                oi = float(tick["open_interest_usd"]) if tick and tick.get("open_interest_usd") is not None else None
                if news.record_mark(
                    event_id=event_id,
                    mark=mark,
                    symbol=symbol,
                    market_type="cex",
                    price=price,
                    open_interest=oi,
                    price_change_pct=_pct(base["price"], price) if base and price is not None else None,
                    oi_change_pct=_pct(base["open_interest"], oi) if base and oi is not None else None,
                    captured_at_ms=now_ms,
                ):
                    written += 1
    return written


__all__ = ["MARK_OFFSETS_MS", "capture_due_marks"]
