from __future__ import annotations

from typing import Any

from tracefold.market.views.stocks_radar_query import StocksRadarQuery
from tracefold.platform.validation import require_nonnegative_int

from .asset_flow_service import WINDOW_MS


class StocksRadarService:
    def __init__(
        self,
        *,
        conn: Any,
        stock_rows_query: Any | None = None,
    ) -> None:
        self.stock_rows_query = stock_rows_query or StocksRadarQuery(conn)

    def stocks_radar(
        self,
        *,
        window: str,
        limit: int,
        now_ms: int,
    ) -> dict[str, Any]:
        parsed_limit = require_nonnegative_int(limit, error_code="stocks_radar_limit_required")
        since_ms = int(now_ms) - WINDOW_MS[window]
        rows = self.stock_rows_query.stock_rows(
            window=window,
            limit=parsed_limit,
        )
        items = [
            _public_row(row, quote=_unavailable_quote("quote_read_model_unavailable", symbol=_row_symbol(row)))
            for row in rows
        ]
        quote_ready_count = sum(1 for item in items if item["quote"]["status"] == "ready")
        quote_unavailable_count = len(items) - quote_ready_count
        return {
            "window": window,
            "query": {
                "window": window,
                "limit": parsed_limit,
                "window_start_ms": since_ms,
                "window_end_ms": int(now_ms),
            },
            "rows": items,
            "health": {
                "returned_count": len(items),
                "quote_ready_count": quote_ready_count,
                "quote_unavailable_count": quote_unavailable_count,
            },
        }


def _public_row(row: dict[str, Any], *, quote: dict[str, Any]) -> dict[str, Any]:
    return {
        "target": {
            "target_type": "MarketInstrument",
            "target_id": row.get("target_id"),
            "symbol": row.get("symbol"),
            "market": "us_equity",
            "exchange": row.get("exchange"),
            "instrument_type": row.get("instrument_type"),
            "name": row.get("security_name"),
        },
        "attention": {
            "mentions": int(row.get("mentions") or 0),
            "unique_authors": int(row.get("unique_authors") or 0),
            "latest_seen_ms": _int_or_none(row.get("latest_seen_ms")),
        },
        "latest_event": {
            "event_id": row.get("latest_event_id"),
            "author_handle": row.get("latest_author_handle"),
            "text": row.get("latest_text"),
            "received_at_ms": _int_or_none(row.get("latest_seen_ms")),
        },
        "quote": quote,
        "source_event_ids": [str(value) for value in (row.get("source_event_ids") or []) if value],
        "row_health": [] if quote.get("status") == "ready" else ["quote_unavailable"],
    }


def _unavailable_quote(error: str, *, symbol: str | None = None) -> dict[str, Any]:
    return {
        "status": "unavailable",
        "price": None,
        "reference_close_price": None,
        "change_pct": None,
        "asof": None,
        "provider": None,
        "provider_symbol": symbol,
        "latency_class": None,
        "freshness_class": None,
        "error": error,
    }


def _row_symbol(row: dict[str, Any]) -> str | None:
    symbol = row.get("symbol")
    if symbol is None:
        return None
    normalized = str(symbol).strip().upper()
    return normalized or None


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
