"""Versioned, deterministic feature extraction from frozen raw results."""

from __future__ import annotations

from decimal import Decimal
from itertools import pairwise
from statistics import pstdev
from typing import Any

from .marketdata import MarketDataResult

PROFILE_VERSION = "evidence_profile_v1"


def _change_bps(rows: tuple[dict[str, Any], ...], interval_count: int) -> int | None:
    if len(rows) <= interval_count:
        return None
    before = Decimal(str(rows[-interval_count - 1]["close"]))
    after = Decimal(str(rows[-1]["close"]))
    if before <= 0:
        return None
    return int((after / before - 1) * 10_000)


def _volatility_bps(rows: tuple[dict[str, Any], ...]) -> int | None:
    closes = [float(row["close"]) for row in rows]
    if len(closes) < 30 or any(value <= 0 for value in closes):
        return None
    returns = [(b / a - 1) * 10_000 for a, b in pairwise(closes)]
    return round(pstdev(returns))


def _taker_share_bps(rows: tuple[dict[str, Any], ...]) -> int | None:
    if not rows:
        return None
    total = sum(Decimal(str(row["quote_volume"])) for row in rows)
    buy = sum(Decimal(str(row["taker_buy_quote_volume"])) for row in rows)
    return int(buy * 10_000 / total) if total > 0 else None


def extract_features(results: dict[str, MarketDataResult], source_fact: dict[str, Any]) -> dict[str, Any]:
    """Missing components stay unknown; source venue and market venue stay distinct."""

    perp = results["perp_bars"]
    spot = results["spot_bars"]
    market = results["market_bars"]
    oi = results["open_interest"]
    funding = results["funding_basis"]
    perp_rows = perp.payload if perp.status == "ok" else ()
    spot_rows = spot.payload if spot.status == "ok" else ()
    market_rows = market.payload if market.status == "ok" else ()
    premium_bps: int | None = None
    funding_bps: int | None = None
    if funding.status == "ok" and funding.payload:
        row = funding.payload[0]
        mark = Decimal(str(row["mark_price"]))
        index = Decimal(str(row["index_price"]))
        premium_bps = int((mark / index - 1) * 10_000) if index > 0 else None
        funding_bps = int(Decimal(str(row["last_funding_rate"])) * 10_000)
    return {
        "profile_version": PROFILE_VERSION,
        "source_kind": source_fact.get("kind"),
        "source_venue": source_fact.get("source_venue"),
        "source_oi_change_bps": source_fact.get("oi_change_bps"),
        "source_oi_value_usd": source_fact.get("oi_value_usd"),
        "perp_return_15m_bps": _change_bps(perp_rows, 15),
        "perp_return_60m_bps": _change_bps(perp_rows, 60),
        "perp_return_240m_bps": _change_bps(perp_rows, 240),
        "perp_volatility_1m_bps": _volatility_bps(perp_rows),
        "perp_taker_buy_share_60m_bps": _taker_share_bps(perp_rows[-60:]),
        "spot_return_60m_bps": _change_bps(spot_rows, 60),
        "spot_taker_buy_share_60m_bps": _taker_share_bps(spot_rows[-60:]),
        "binance_open_interest_quantity": (
            oi.payload[0].get("open_interest_quantity") if oi.status == "ok" and oi.payload else None
        ),
        "funding_rate_bps": funding_bps,
        "premium_bps": premium_bps,
        "btc_return_60m_bps": _change_bps(market_rows, 60),
        "data_status": {name: result.status for name, result in results.items()},
    }
