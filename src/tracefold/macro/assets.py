from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CrossAssetInstrument:
    symbol: str
    daily_dataset_id: str
    intraday_dataset_id: str

    @property
    def dataset_pair(self) -> tuple[str, str]:
        return (self.daily_dataset_id, self.intraday_dataset_id)


@dataclass(frozen=True, slots=True)
class CrossAssetDatasetGroup:
    group_id: str
    label: str
    instruments: tuple[CrossAssetInstrument, ...]

    @property
    def daily_dataset_ids(self) -> tuple[str, ...]:
        return tuple(instrument.daily_dataset_id for instrument in self.instruments)

    @property
    def intraday_dataset_ids(self) -> tuple[str, ...]:
        return tuple(instrument.intraday_dataset_id for instrument in self.instruments)

    @property
    def dataset_pairs(self) -> tuple[tuple[str, str], ...]:
        return tuple(instrument.dataset_pair for instrument in self.instruments)


@dataclass(frozen=True, slots=True)
class CrossAssetDatasetCatalog:
    etf_groups: tuple[CrossAssetDatasetGroup, ...]
    futures_group: CrossAssetDatasetGroup
    benchmark_change_dataset_ids: tuple[str, ...]

    @property
    def etf_instruments(self) -> tuple[CrossAssetInstrument, ...]:
        return tuple(instrument for group in self.etf_groups for instrument in group.instruments)

    @property
    def etf_daily_dataset_ids(self) -> tuple[str, ...]:
        return tuple(instrument.daily_dataset_id for instrument in self.etf_instruments)

    @property
    def etf_intraday_dataset_ids(self) -> tuple[str, ...]:
        return tuple(instrument.intraday_dataset_id for instrument in self.etf_instruments)

    @property
    def etf_dataset_pairs(self) -> tuple[tuple[str, str], ...]:
        return tuple(instrument.dataset_pair for instrument in self.etf_instruments)

    @property
    def etf_normalized_groups(self) -> tuple[tuple[str, str, tuple[str, ...]], ...]:
        return tuple((group.group_id, group.label, group.daily_dataset_ids) for group in self.etf_groups)

    @property
    def futures_daily_dataset_ids(self) -> tuple[str, ...]:
        return self.futures_group.daily_dataset_ids

    @property
    def futures_intraday_dataset_ids(self) -> tuple[str, ...]:
        return self.futures_group.intraday_dataset_ids

    @property
    def futures_dataset_pairs(self) -> tuple[tuple[str, str], ...]:
        return self.futures_group.dataset_pairs

    @property
    def five_year_backfill_dataset_ids(self) -> frozenset[str]:
        return frozenset((*self.etf_daily_dataset_ids, *self.futures_daily_dataset_ids))

    @property
    def asset_change_dataset_ids(self) -> frozenset[str]:
        return frozenset((*self.etf_daily_dataset_ids, *self.benchmark_change_dataset_ids))

    def etf_instrument(self, symbol: str) -> CrossAssetInstrument:
        for instrument in self.etf_instruments:
            if instrument.symbol == symbol:
                return instrument
        raise ValueError(f"unknown_cross_asset_etf:{symbol}")


CROSS_ASSET_DATASETS = CrossAssetDatasetCatalog(
    etf_groups=(
        CrossAssetDatasetGroup(
            group_id="equity",
            label="权益",
            instruments=(
                CrossAssetInstrument("SPY", "nasdaq.spy.daily", "yfinance.spy.intraday"),
                CrossAssetInstrument("QQQ", "nasdaq.qqq.daily", "yfinance.qqq.intraday"),
                CrossAssetInstrument("IWM", "nasdaq.iwm.daily", "yfinance.iwm.intraday"),
            ),
        ),
        CrossAssetDatasetGroup(
            group_id="duration_credit",
            label="久期与信用",
            instruments=(
                CrossAssetInstrument("TLT", "nasdaq.tlt.daily", "yfinance.tlt.intraday"),
                CrossAssetInstrument("IEF", "nasdaq.ief.daily", "yfinance.ief.intraday"),
                CrossAssetInstrument("LQD", "nasdaq.lqd.daily", "yfinance.lqd.intraday"),
                CrossAssetInstrument("HYG", "nasdaq.hyg.daily", "yfinance.hyg.intraday"),
            ),
        ),
        CrossAssetDatasetGroup(
            group_id="dollar_commodities",
            label="美元与商品",
            instruments=(
                CrossAssetInstrument("UUP", "nasdaq.dxy.daily", "yfinance.dxy.intraday"),
                CrossAssetInstrument("GLD", "nasdaq.gld.daily", "yfinance.gld.intraday"),
                CrossAssetInstrument("USO", "nasdaq.uso.daily", "yfinance.uso.intraday"),
            ),
        ),
    ),
    futures_group=CrossAssetDatasetGroup(
        group_id="major_futures",
        label="主要连续期货",
        instruments=(
            CrossAssetInstrument("ES", "yfinance.es_future.daily", "yfinance.es_future.intraday"),
            CrossAssetInstrument("NQ", "yfinance.nq_future.daily", "yfinance.nq_future.intraday"),
            CrossAssetInstrument("RTY", "yfinance.rty_future.daily", "yfinance.rty_future.intraday"),
            CrossAssetInstrument("ZB", "yfinance.zb_future.daily", "yfinance.zb_future.intraday"),
            CrossAssetInstrument("ZN", "yfinance.zn_future.daily", "yfinance.zn_future.intraday"),
            CrossAssetInstrument("DXY", "yfinance.dx_future.daily", "yfinance.dx_future.intraday"),
            CrossAssetInstrument("GC", "yfinance.gc_future.daily", "yfinance.gc_future.intraday"),
            CrossAssetInstrument("CL", "yfinance.cl_future.daily", "yfinance.cl_future.intraday"),
            CrossAssetInstrument("HG", "yfinance.hg_future.daily", "yfinance.hg_future.intraday"),
        ),
    ),
    benchmark_change_dataset_ids=("binance.btcusdt.spot", "fred.vixcls"),
)

_instruments = (*CROSS_ASSET_DATASETS.etf_instruments, *CROSS_ASSET_DATASETS.futures_group.instruments)
_dataset_ids = tuple(dataset_id for instrument in _instruments for dataset_id in instrument.dataset_pair)
if len(_dataset_ids) != len(set(_dataset_ids)):
    raise RuntimeError("cross_asset_dataset_catalog_duplicate_dataset")
if any(not group.instruments for group in (*CROSS_ASSET_DATASETS.etf_groups, CROSS_ASSET_DATASETS.futures_group)):
    raise RuntimeError("cross_asset_dataset_catalog_empty_group")


__all__ = [
    "CROSS_ASSET_DATASETS",
    "CrossAssetDatasetCatalog",
    "CrossAssetDatasetGroup",
    "CrossAssetInstrument",
]
