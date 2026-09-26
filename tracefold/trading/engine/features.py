"""Versioned, deterministic feature extraction from frozen raw results."""

from __future__ import annotations

from decimal import Decimal
from itertools import pairwise
from typing import Any, Literal, cast

from .contracts import FeatureValue, FrozenEvidence
from .marketdata import MarketDataResult

PROFILE_VERSION = "evidence_profile_v3"
_BAR_MS = 60_000


def catalyst_text_values(source_fact: dict[str, Any]) -> dict[str, str]:
    """Read the public catalyst text used by both evidence and qualification.

    News maps its internal editorial fields to headline/why in the outbox.
    Other spellings are not public aliases. Keep the original strings for
    evidence; strip only to test whether text is present, never to rewrite it.
    """
    if source_fact.get("kind") != "catalyst":
        return {}
    values: dict[str, str] = {}
    for key in ("headline", "why"):
        value = source_fact.get(key)
        if isinstance(value, str) and value.strip():
            values[key] = value
    return values


def _change_bps(rows: tuple[dict[str, Any], ...], interval_count: int) -> str | None:
    if len(rows) <= interval_count:
        return None
    window = rows[-interval_count - 1 :]
    if not _continuous(window):
        return None
    closes = [Decimal(str(row["close"])) for row in window]
    if any(not value.is_finite() or value <= 0 for value in closes):
        return None
    before, after = closes[0], closes[-1]
    return str((after / before - 1) * 10_000)


def _volatility_bps(rows: tuple[dict[str, Any], ...]) -> str | None:
    if not _continuous(rows):
        return None
    closes = [Decimal(str(row["close"])) for row in rows]
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


def _taker_share_bps(rows: tuple[dict[str, Any], ...], interval_count: int) -> str | None:
    if len(rows) < interval_count:
        return None
    window = rows[-interval_count:]
    if not _continuous(window):
        return None
    totals = [Decimal(str(row["quote_volume"])) for row in window]
    buys = [Decimal(str(row["taker_buy_quote_volume"])) for row in window]
    if any(not value.is_finite() or value < 0 for value in (*totals, *buys)) or any(
        buy > volume for buy, volume in zip(buys, totals, strict=True)
    ):
        return None
    total = sum(totals)
    buy = sum(buys)
    return str(buy * 10_000 / total) if total > 0 and buy <= total else None


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
    premium_bps: str | None = None
    funding_bps: str | None = None
    if funding.status == "ok" and funding.payload:
        row = funding.payload[0]
        mark = Decimal(str(row["mark_price"]))
        index = Decimal(str(row["index_price"]))
        rate = Decimal(str(row["last_funding_rate"]))
        premium_bps = str((mark / index - 1) * 10_000) if mark.is_finite() and index.is_finite() and index > 0 else None
        funding_bps = str(rate * 10_000) if rate.is_finite() else None
    return {
        "profile_version": PROFILE_VERSION,
        "source_kind": source_fact.get("kind"),
        "source_venue": source_fact.get("source_venue"),
        "source_oi_change_bps": source_fact.get("oi_change_bps"),
        "source_oi_value_usd": source_fact.get("oi_value_usd"),
        "source_oi_measurement_definition": source_fact.get("measurement_definition"),
        "perp_return_15m_bps": _change_bps(perp_rows, 15),
        "perp_return_60m_bps": _change_bps(perp_rows, 60),
        "perp_return_240m_bps": _change_bps(perp_rows, 240),
        "perp_volatility_1m_bps": _volatility_bps(perp_rows),
        "perp_taker_buy_share_15m_bps": _taker_share_bps(perp_rows, 15),
        "perp_taker_buy_share_60m_bps": _taker_share_bps(perp_rows, 60),
        "spot_return_60m_bps": _change_bps(spot_rows, 60),
        "spot_taker_buy_share_60m_bps": _taker_share_bps(spot_rows, 60),
        "binance_open_interest_quantity": (
            oi.payload[0].get("open_interest_quantity") if oi.status == "ok" and oi.payload else None
        ),
        "funding_rate_bps": funding_bps,
        "premium_bps": premium_bps,
        "btc_return_60m_bps": _change_bps(market_rows, 60),
        "data_status": {name: result.status for name, result in results.items()},
    }


def freeze_features(
    *,
    snapshot_ref: str,
    knowledge_cutoff_ms: int,
    data_environment: str,
    source_first_visible_at_ms: int,
    source_fact: dict[str, Any],
    results: dict[str, MarketDataResult],
    features: dict[str, Any],
) -> FrozenEvidence:
    def frame_name(feature_id: str) -> str | None:
        if feature_id.startswith("source_"):
            return None
        if feature_id.startswith("perp_"):
            return "perp_bars"
        if feature_id.startswith("spot_"):
            return "spot_bars"
        if feature_id.startswith("btc_"):
            return "market_bars"
        if feature_id == "binance_open_interest_quantity":
            return "open_interest"
        if feature_id in ("funding_rate_bps", "premium_bps"):
            return "funding_basis"
        raise ValueError("feature_source_unknown")

    values: list[FeatureValue] = []
    for feature_id, raw in features.items():
        if feature_id in ("profile_version", "source_kind", "source_venue", "data_status"):
            continue
        frame = frame_name(feature_id)
        event_at: int | None
        received_at: int | None
        if frame is None:
            source_event_at = source_fact.get("source_recorded_at_ms")
            event_at = source_event_at if isinstance(source_event_at, int) else None
            received_at = source_first_visible_at_ms
            source_ok = isinstance(event_at, int) and isinstance(received_at, int)
        else:
            result = results[frame]
            event_at = result.event_end_ms
            received_at = result.received_at_ms
            source_ok = result.status == "ok"
        available = (
            raw is not None
            and source_ok
            and isinstance(event_at, int)
            and isinstance(received_at, int)
            and event_at <= knowledge_cutoff_ms
            and received_at <= knowledge_cutoff_ms
        )
        unit = (
            "bps"
            if feature_id.endswith("_bps")
            else "USD"
            if feature_id.endswith("_usd")
            else "native_contract_quantity"
            if feature_id.endswith("_quantity")
            else "text"
        )
        values.append(
            FeatureValue(
                feature_id=feature_id,
                value=str(raw) if available else None,
                unit=unit,
                status="ok" if available else "missing",
                source_ref=snapshot_ref,
                event_at_ms=event_at if isinstance(event_at, int) else None,
                received_at_ms=received_at if isinstance(received_at, int) else None,
                feature_version=PROFILE_VERSION,
            )
        )
    if data_environment not in ("live", "demo", "testnet"):
        raise ValueError("evidence_environment_invalid")
    return FrozenEvidence(
        snapshot_ref=snapshot_ref,
        knowledge_cutoff_ms=knowledge_cutoff_ms,
        data_environment=cast(Literal["live", "demo", "testnet"], data_environment),
        values=tuple(values),
    )
