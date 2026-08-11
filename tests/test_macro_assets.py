from __future__ import annotations

from datetime import date

from tracefold.macro.assets import CROSS_ASSET_DATASETS
from tracefold.macro.backfill import professional_backfill_policies
from tracefold.macro.calculations import CALCULATION_REGISTRY
from tracefold.macro.coverage import COVERAGE_MANIFEST


def test_cross_asset_dataset_catalog_owns_exact_instrument_pairs_and_groups() -> None:
    assert [group.group_id for group in CROSS_ASSET_DATASETS.etf_groups] == [
        "equity",
        "duration_credit",
        "dollar_commodities",
    ]
    assert [group.label for group in CROSS_ASSET_DATASETS.etf_groups] == [
        "权益",
        "久期与信用",
        "美元与商品",
    ]
    assert [instrument.symbol for instrument in CROSS_ASSET_DATASETS.etf_instruments] == [
        "SPY",
        "QQQ",
        "IWM",
        "TLT",
        "IEF",
        "LQD",
        "HYG",
        "UUP",
        "GLD",
        "USO",
    ]
    assert CROSS_ASSET_DATASETS.etf_dataset_pairs == (
        ("nasdaq.spy.daily", "yfinance.spy.intraday"),
        ("nasdaq.qqq.daily", "yfinance.qqq.intraday"),
        ("nasdaq.iwm.daily", "yfinance.iwm.intraday"),
        ("nasdaq.tlt.daily", "yfinance.tlt.intraday"),
        ("nasdaq.ief.daily", "yfinance.ief.intraday"),
        ("nasdaq.lqd.daily", "yfinance.lqd.intraday"),
        ("nasdaq.hyg.daily", "yfinance.hyg.intraday"),
        ("nasdaq.dxy.daily", "yfinance.dxy.intraday"),
        ("nasdaq.gld.daily", "yfinance.gld.intraday"),
        ("nasdaq.uso.daily", "yfinance.uso.intraday"),
    )
    assert [instrument.symbol for instrument in CROSS_ASSET_DATASETS.futures_group.instruments] == [
        "ES",
        "NQ",
        "RTY",
        "ZB",
        "ZN",
        "DXY",
        "GC",
        "CL",
        "HG",
    ]
    assert CROSS_ASSET_DATASETS.futures_dataset_pairs == (
        ("yfinance.es_future.daily", "yfinance.es_future.intraday"),
        ("yfinance.nq_future.daily", "yfinance.nq_future.intraday"),
        ("yfinance.rty_future.daily", "yfinance.rty_future.intraday"),
        ("yfinance.zb_future.daily", "yfinance.zb_future.intraday"),
        ("yfinance.zn_future.daily", "yfinance.zn_future.intraday"),
        ("yfinance.dx_future.daily", "yfinance.dx_future.intraday"),
        ("yfinance.gc_future.daily", "yfinance.gc_future.intraday"),
        ("yfinance.cl_future.daily", "yfinance.cl_future.intraday"),
        ("yfinance.hg_future.daily", "yfinance.hg_future.intraday"),
    )
    assert CROSS_ASSET_DATASETS.five_year_backfill_dataset_ids == frozenset(
        {
            "nasdaq.spy.daily",
            "nasdaq.qqq.daily",
            "nasdaq.iwm.daily",
            "nasdaq.tlt.daily",
            "nasdaq.ief.daily",
            "nasdaq.lqd.daily",
            "nasdaq.hyg.daily",
            "nasdaq.dxy.daily",
            "nasdaq.gld.daily",
            "nasdaq.uso.daily",
            "yfinance.es_future.daily",
            "yfinance.nq_future.daily",
            "yfinance.rty_future.daily",
            "yfinance.zb_future.daily",
            "yfinance.zn_future.daily",
            "yfinance.dx_future.daily",
            "yfinance.gc_future.daily",
            "yfinance.cl_future.daily",
            "yfinance.hg_future.daily",
        }
    )


def test_cross_asset_consumers_share_the_catalog_exact_sets() -> None:
    assert COVERAGE_MANIFEST["cross_asset.etf_matrix"].dataset_ids == (CROSS_ASSET_DATASETS.etf_intraday_dataset_ids)
    assert COVERAGE_MANIFEST["cross_asset.etf_daily_history"].dataset_ids == (
        CROSS_ASSET_DATASETS.etf_daily_dataset_ids
    )
    assert COVERAGE_MANIFEST["cross_asset.major_futures_market"].dataset_ids == (
        CROSS_ASSET_DATASETS.futures_intraday_dataset_ids
    )
    assert COVERAGE_MANIFEST["cross_asset.major_futures_daily_history"].dataset_ids == (
        CROSS_ASSET_DATASETS.futures_daily_dataset_ids
    )
    assert CALCULATION_REGISTRY["cross_asset.normalized_returns"].input_dataset_ids == (
        CROSS_ASSET_DATASETS.etf_daily_dataset_ids
    )
    assert CALCULATION_REGISTRY["cross_asset.return_correlations"].input_dataset_ids == (
        CROSS_ASSET_DATASETS.etf_daily_dataset_ids
    )

    policies = {policy.dataset_id: policy for policy in professional_backfill_policies(through_date=date(2026, 8, 11))}
    assert CROSS_ASSET_DATASETS.five_year_backfill_dataset_ids <= policies.keys()
    assert all(
        policies[dataset_id].start_date == date(2021, 8, 11)
        for dataset_id in CROSS_ASSET_DATASETS.five_year_backfill_dataset_ids
    )
