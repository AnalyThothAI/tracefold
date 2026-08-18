from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class BinanceUsdtPerpRoute:
    native_market_id: str
    base_symbol: str
    quote_symbol: str
    multiplier: float | None


def sync_binance_usdt_perp_routes(
    *,
    repos: Any,
    routes: Iterable[BinanceUsdtPerpRoute],
    observed_at_ms: int,
    dry_run: bool,
    execute: bool,
) -> dict[str, Any]:
    if dry_run == execute:
        raise ValueError("exactly one of dry_run or execute must be true")

    started = time.monotonic()
    routes = _normalized_routes(routes)
    base_symbols = [route.base_symbol for route in routes]
    native_market_ids = [route.native_market_id for route in routes]
    registry = repos.registry
    plan = dict(
        registry.binance_usdt_perp_sync_plan_counts(
            base_symbols=base_symbols,
            native_market_ids=native_market_ids,
        )
    )

    cex_tokens_written = 0
    pricefeeds_written = 0
    affected_lookup_keys: set[str] = set()
    if execute:
        with repos.transaction():
            for route in routes:
                cex_token = registry.upsert_cex_token(
                    base_symbol=route.base_symbol,
                    source="binance_cex",
                    observed_at_ms=observed_at_ms,
                )
                cex_tokens_written += 1
                registry.upsert_pricefeed(
                    feed_type="cex_swap",
                    provider="binance",
                    subject_type="CexToken",
                    subject_id=str(cex_token["cex_token_id"]),
                    native_market_id=route.native_market_id,
                    base_cex_token_id=str(cex_token["cex_token_id"]),
                    base_symbol=route.base_symbol,
                    quote_symbol=route.quote_symbol,
                    multiplier=route.multiplier,
                    observed_at_ms=observed_at_ms,
                )
                pricefeeds_written += 1
                affected_lookup_keys.update(_symbol_lookup_keys(route.base_symbol))

    return {
        "mode": "execute" if execute else "dry_run",
        "provider": "binance",
        "feed_type": "cex_swap",
        "quote_symbol": "USDT",
        "contract_type": "PERPETUAL",
        "binance_usdt_perp_seen": len(routes),
        "cex_tokens_to_insert": int(plan.get("cex_tokens_to_insert", len(routes))),
        "cex_tokens_to_delete": int(plan.get("cex_tokens_to_delete", 0)),
        "pricefeeds_to_insert": int(plan.get("pricefeeds_to_insert", len(routes))),
        "cex_tokens_written": cex_tokens_written,
        "pricefeeds_written": pricefeeds_written,
        "affected_lookup_keys": sorted(affected_lookup_keys),
        "duration_ms": int((time.monotonic() - started) * 1000),
    }


def _normalized_routes(routes: Iterable[BinanceUsdtPerpRoute]) -> list[BinanceUsdtPerpRoute]:
    by_market_id: dict[str, BinanceUsdtPerpRoute] = {}
    for route in routes:
        if not isinstance(route, BinanceUsdtPerpRoute):
            raise RuntimeError("asset_market_sync_binance_route_contract_required")
        native_market_id = _symbol(route.native_market_id)
        base_symbol = _symbol(route.base_symbol)
        quote_symbol = _symbol(route.quote_symbol)
        if not native_market_id or not base_symbol or quote_symbol != "USDT":
            continue
        by_market_id[native_market_id] = BinanceUsdtPerpRoute(
            native_market_id=native_market_id,
            base_symbol=base_symbol,
            quote_symbol=quote_symbol,
            multiplier=route.multiplier,
        )
    return [by_market_id[key] for key in sorted(by_market_id)]


def _symbol(value: Any) -> str:
    return str(value or "").strip().lstrip("$").upper()


def _symbol_lookup_keys(symbol: Any) -> set[str]:
    normalized = str(symbol or "").strip().lstrip("$").upper()
    if not normalized:
        return set()
    return {f"symbol:{normalized}", f"project_symbol:{normalized}", f"cex_token:{normalized}"}
