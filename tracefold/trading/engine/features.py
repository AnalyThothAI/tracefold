"""Versioned, deterministic feature extraction from frozen raw results."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal, InvalidOperation
from itertools import pairwise
from typing import Any

from .marketdata import MarketDataResult

PROFILE_VERSION = "evidence_profile_v4"
_BAR_MS = 60_000
WINDOW_VERSION = "closed_bar_window_v1"


CATALYST_SOURCE_KIND = "catalyst_delta"


def catalyst_text_values(source_fact: dict[str, Any]) -> dict[str, str]:
    """Read the public catalyst text used by both evidence and qualification.

    A News catalyst delta carries `text`: the deterministic projection of the
    changed claims, their fields, citations and source relationships. It is
    neither a ReaderCard nor a model-written trading summary, and no other key
    is a public alias. Keep the original string for evidence; strip only to
    test whether text is present, never to rewrite it.
    """
    if source_fact.get("kind") != CATALYST_SOURCE_KIND:
        return {}
    value = source_fact.get("text")
    return {"text": value} if isinstance(value, str) and value.strip() else {}


def source_recorded_at_ms(source_fact: dict[str, Any]) -> int | None:
    """When the producer recorded this public fact; never a later relay or model clock.

    A catalyst delta is recorded when its semantic result completed; its
    freshness (`first_available_at_ms`) is a separate, earlier clock.
    """
    key = "semantic_completed_at_ms" if source_fact.get("kind") == CATALYST_SOURCE_KIND else "source_recorded_at_ms"
    value = source_fact.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _change_bps(rows: tuple[dict[str, Any], ...], interval_count: int) -> str | None:
    if len(rows) <= interval_count:
        return None
    window = rows[-interval_count - 1 :]
    if not _continuous(window):
        return None
    try:
        closes = [Decimal(str(row["close"])) for row in window]
    except (KeyError, TypeError, InvalidOperation):
        return None
    if any(not value.is_finite() or value <= 0 for value in closes):
        return None
    before, after = closes[0], closes[-1]
    return str((after / before - 1) * 10_000)


def _volatility_bps(rows: tuple[dict[str, Any], ...]) -> str | None:
    if not _continuous(rows):
        return None
    try:
        closes = [Decimal(str(row["close"])) for row in rows]
    except (KeyError, TypeError, InvalidOperation):
        return None
    if len(closes) < 30 or any(not value.is_finite() or value <= 0 for value in closes):
        return None
    returns = [(b / a - 1) * 10_000 for a, b in pairwise(closes)]
    mean = sum(returns, Decimal(0)) / len(returns)
    variance = sum(((value - mean) ** 2 for value in returns), Decimal(0)) / len(returns)
    return str(variance.sqrt())


def _continuous(rows: tuple[dict[str, Any], ...]) -> bool:
    if not rows:
        return False
    stamps = [int(row["event_at_ms"]) for row in rows]
    return all(right - left == _BAR_MS for left, right in pairwise(stamps))


def closed_bar_window(
    result: MarketDataResult,
    *,
    count: int,
    end_ms: int | None = None,
    cutoff_ms: int | None = None,
    source_identity: str | None = None,
    unit_definition: str | None = None,
) -> tuple[dict[str, Any], ...]:
    """Prove one exact closed interval from the original response, including partial responses."""
    if result.status not in ("ok", "partial") or count < 1:
        return ()
    if source_identity is not None and result.source_identity != source_identity:
        return ()
    if unit_definition is not None and result.unit_definition != unit_definition:
        return ()
    if result.received_at_ms is None or (cutoff_ms is not None and result.received_at_ms > cutoff_ms):
        return ()
    rows = result.payload[-count:]
    if len(rows) != count:
        return ()
    try:
        stamps = [int(row["event_at_ms"]) for row in rows]
        received = [int(row.get("received_at_ms", result.received_at_ms)) for row in rows]
        opens = [row.get("open_at_ms") for row in rows]
    except (KeyError, TypeError, ValueError):
        return ()
    expected_end = result.event_end_ms if end_ms is None else end_ms
    if expected_end is None or stamps[-1] != expected_end or stamps[-1] % _BAR_MS:
        return ()
    if not _continuous(rows) or result.event_end_ms != stamps[-1]:
        return ()
    if any(
        stamp > receipt or (cutoff_ms is not None and receipt > cutoff_ms)
        for stamp, receipt in zip(stamps, received, strict=True)
    ):
        return ()
    try:
        if any(
            open_at is not None and int(open_at) != stamp - _BAR_MS
            for open_at, stamp in zip(opens, stamps, strict=True)
        ):
            return ()
    except (TypeError, ValueError):
        return ()
    return rows


def window_ref(
    *, dataset: str, source: str, unit: str, environment: str, symbol: str, rows: tuple[dict[str, Any], ...]
) -> str:
    """The same consumed bars have one identity regardless of archive or observation channel."""
    stable = [{key: row[key] for key in ("event_at_ms", "high", "low", "close")} for row in rows]
    payload = json.dumps(
        (WINDOW_VERSION, dataset, source, unit, environment, symbol, stable),
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return f"market:{dataset}:window:{hashlib.sha256(payload.encode()).hexdigest()}"


def price_plan_window(
    result: MarketDataResult,
    *,
    end_ms: int,
    cutoff_ms: int,
    source_identity: str,
    unit_definition: str,
) -> tuple[dict[str, Any], ...]:
    """Validate the exact 16 closed bars consumed by a plan before naming their projection."""
    rows = closed_bar_window(
        result,
        count=16,
        end_ms=end_ms,
        cutoff_ms=cutoff_ms,
        source_identity=source_identity,
        unit_definition=unit_definition,
    )
    try:
        prices = [tuple(Decimal(str(row[key])) for key in ("high", "low", "close")) for row in rows]
    except (KeyError, TypeError, InvalidOperation):
        return ()
    return (
        rows
        if len(rows) == 16
        and all(
            all(value.is_finite() and value > 0 for value in price) and price[0] >= price[2] >= price[1]
            for price in prices
        )
        else ()
    )


def _taker_share_bps(rows: tuple[dict[str, Any], ...], interval_count: int) -> str | None:
    if len(rows) < interval_count:
        return None
    window = rows[-interval_count:]
    if not _continuous(window):
        return None
    try:
        totals = [Decimal(str(row["quote_volume"])) for row in window]
        buys = [Decimal(str(row["taker_buy_quote_volume"])) for row in window]
    except (KeyError, TypeError, InvalidOperation):
        return None
    if any(not value.is_finite() or value < 0 for value in (*totals, *buys)) or any(
        buy > volume for buy, volume in zip(buys, totals, strict=True)
    ):
        return None
    total = sum(totals)
    buy = sum(buys)
    return str(buy * 10_000 / total) if total > 0 and buy <= total else None


def extract_features(
    results: dict[str, MarketDataResult],
    source_fact: dict[str, Any],
    *,
    expected_ends: dict[str, int] | None = None,
    cutoff_ms: int | None = None,
) -> dict[str, Any]:
    """Missing components stay unknown; source venue and market venue stay distinct."""

    oi = results["open_interest"]
    funding = results["funding_basis"]

    def bars(name: str, count: int) -> tuple[dict[str, Any], ...]:
        result = results[name]
        return closed_bar_window(
            result,
            count=count,
            end_ms=None if expected_ends is None else expected_ends.get(name),
            cutoff_ms=cutoff_ms,
        )

    premium_bps: str | None = None
    funding_bps: str | None = None
    if funding.status == "ok" and funding.payload:
        row = funding.payload[0]
        try:
            mark = Decimal(str(row["mark_price"]))
            index = Decimal(str(row["index_price"]))
            rate = Decimal(str(row["last_funding_rate"]))
            premium_bps = (
                str((mark / index - 1) * 10_000)
                if mark.is_finite() and mark > 0 and index.is_finite() and index > 0
                else None
            )
            funding_bps = str(rate * 10_000) if rate.is_finite() else None
        except (KeyError, TypeError, InvalidOperation):
            pass
    oi_quantity: str | None = None
    if oi.status == "ok" and oi.payload:
        try:
            value = Decimal(str(oi.payload[0]["open_interest_quantity"]))
            oi_quantity = str(value) if value.is_finite() and value >= 0 else None
        except (KeyError, TypeError, InvalidOperation):
            pass
    return {
        "profile_version": PROFILE_VERSION,
        "source_kind": source_fact.get("kind"),
        "source_venue": source_fact.get("source_venue"),
        "source_oi_change_bps": source_fact.get("oi_change_bps"),
        "source_oi_value_usd": source_fact.get("oi_value_usd"),
        "source_oi_measurement_definition": source_fact.get("measurement_definition"),
        "perp_return_15m_bps": _change_bps(bars("perp_bars", 16), 15),
        "perp_return_60m_bps": _change_bps(bars("perp_bars", 61), 60),
        "perp_return_240m_bps": _change_bps(bars("perp_bars", 241), 240),
        "perp_volatility_1m_bps": _volatility_bps(bars("perp_bars", 241)),
        "perp_taker_buy_share_15m_bps": _taker_share_bps(bars("perp_bars", 15), 15),
        "perp_taker_buy_share_60m_bps": _taker_share_bps(bars("perp_bars", 60), 60),
        "spot_return_60m_bps": _change_bps(bars("spot_bars", 61), 60),
        "spot_taker_buy_share_60m_bps": _taker_share_bps(bars("spot_bars", 60), 60),
        "binance_open_interest_quantity": oi_quantity,
        "funding_rate_bps": funding_bps,
        "premium_bps": premium_bps,
        "btc_return_60m_bps": _change_bps(bars("market_bars", 61), 60),
        "data_status": {name: result.status for name, result in results.items()},
    }
