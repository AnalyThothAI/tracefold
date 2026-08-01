from __future__ import annotations

MACRO_ASSET_DATASETS: dict[str, str] = {
    "SPY": "nasdaq.spy.daily",
    "QQQ": "nasdaq.qqq.daily",
    "IWM": "nasdaq.iwm.daily",
    "TLT": "nasdaq.tlt.daily",
    "IEF": "nasdaq.ief.daily",
    "LQD": "nasdaq.lqd.daily",
    "HYG": "nasdaq.hyg.daily",
    "UUP": "nasdaq.dxy.daily",
    "GLD": "nasdaq.gld.daily",
    "USO": "nasdaq.uso.daily",
    "BTC": "binance.btcusdt.spot",
    "VIX": "fred.vixcls",
}

MACRO_ASSET_DATASET_IDS: frozenset[str] = frozenset(MACRO_ASSET_DATASETS.values())

__all__ = [
    "MACRO_ASSET_DATASETS",
    "MACRO_ASSET_DATASET_IDS",
]
