"""Pinned closed-adapter contracts owned by the Trading bounded context."""

from __future__ import annotations

from typing import Final

from .contracts import canonical_sha256

NAUTILUS_ADAPTER_RELEASE: Final = {
    "version": "1.231.0",
    "git_commit": "27a8e54e7ac3c57d6cbf8891f0283dfbaee97317",
}
BINANCE_USDM_ADAPTER_CONTRACT: Final = {
    "version": "binance_usdm_demo_adapter_contract_v1",
    "environment": "demo",
    "account_type": "USDT_FUTURES",
    "position_mode": "one_way",
    "leverage": "1",
    "entry": "market_from_committed_fence",
    "submit_retries": 0,
    "timeout": "query_first_manual_review_if_unproven",
    "protection": "native_reduce_only_stop_market",
    "partial_fill": "resize_to_authoritative_filled_quantity",
    "close": "reduce_only_market_then_authoritative_flat",
    "nautilus_release": NAUTILUS_ADAPTER_RELEASE,
}
BINANCE_USDM_ADAPTER_CONTRACT_SHA256: Final = canonical_sha256(BINANCE_USDM_ADAPTER_CONTRACT)

__all__ = [
    "BINANCE_USDM_ADAPTER_CONTRACT",
    "BINANCE_USDM_ADAPTER_CONTRACT_SHA256",
    "NAUTILUS_ADAPTER_RELEASE",
]
