from __future__ import annotations

from types import MappingProxyType

from tracefold.macro.domain import DatasetSpec, MacroModuleId

_FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
_NASDAQ_HISTORY = "https://api.nasdaq.com/api/quote/{symbol}/historical"
_DAILY_FRESHNESS_SECONDS = 172_800
_WEEKLY_FRESHNESS_SECONDS = 950_400
_MONTHLY_FRESHNESS_SECONDS = 7_948_800
_QUARTERLY_FRESHNESS_SECONDS = 20_736_000
_DEFAULT_FRESHNESS_SECONDS = {
    "daily": _DAILY_FRESHNESS_SECONDS,
    "weekly": _WEEKLY_FRESHNESS_SECONDS,
    "monthly": _MONTHLY_FRESHNESS_SECONDS,
    "quarterly": _QUARTERLY_FRESHNESS_SECONDS,
}


def _fred(
    series_id: str,
    *,
    module: MacroModuleId,
    label: str,
    unit: str,
    frequency: str,
    clock: str = "official_state",
    freshness_seconds: int | None = None,
    refresh_seconds: int = 21_600,
    critical: bool = False,
) -> DatasetSpec:
    return DatasetSpec(
        dataset_id=f"fred.{series_id.lower()}",
        module_id=module,
        clock_kind=clock,  # type: ignore[arg-type]
        fact_family="series",
        adapter_id="fred_csv",
        source_id="fred",
        source_url=_FRED_CSV.format(series_id=series_id),
        label=label,
        series_id=series_id,
        unit=unit,
        frequency=frequency,
        freshness_seconds=(
            freshness_seconds
            if freshness_seconds is not None
            else _DEFAULT_FRESHNESS_SECONDS.get(frequency, _DAILY_FRESHNESS_SECONDS)
        ),
        refresh_seconds=refresh_seconds,
        critical=critical,
        metadata={"official_owner": "Federal Reserve Bank of St. Louis"},
    )


def _nasdaq_history(
    symbol: str,
    instrument_id: str,
    *,
    label: str,
    asset_class: str,
) -> DatasetSpec:
    return DatasetSpec(
        dataset_id=f"nasdaq.{instrument_id}.history",
        module_id="cross_asset",
        clock_kind="daily_settlement",
        fact_family="market_observation",
        adapter_id="nasdaq_history",
        source_id="nasdaq_public",
        source_url=_NASDAQ_HISTORY.format(symbol=symbol),
        label=label,
        series_id=symbol,
        unit="price",
        frequency="daily",
        freshness_seconds=_DAILY_FRESHNESS_SECONDS,
        refresh_seconds=21_600,
        trust_tier="untrusted_proxy",
        instrument_id=instrument_id,
        symbol=symbol,
        instrument_name=label,
        asset_class=asset_class,
        instrument_type="etf",
        venue="Nasdaq public website",
        metadata={
            "role": "non_critical_free_public_history",
            "availability": "previous_close",
            "contract": "unsupported_public_website_endpoint",
        },
    )


def _bls_release(
    series_id: str,
    *,
    dataset_id: str,
    label: str,
    unit: str,
    importance_tier: int = 2,
) -> DatasetSpec:
    return DatasetSpec(
        dataset_id=dataset_id,
        module_id="economy_inflation",
        clock_kind="scheduled_release",
        fact_family="release",
        adapter_id="bls_release",
        source_id="bls",
        source_url="https://api.bls.gov/publicAPI/v2/timeseries/data/",
        label=label,
        series_id=series_id,
        unit=unit,
        frequency="monthly",
        freshness_seconds=_MONTHLY_FRESHNESS_SECONDS,
        refresh_seconds=21_600,
        critical=False,
        metadata={
            "official_owner": "U.S. Bureau of Labor Statistics",
            "importance_tier": importance_tier,
            "role": "official_release_catalyst",
        },
    )


def _cftc_tff(
    dataset_id: str,
    *,
    module: MacroModuleId,
    label: str,
    contracts: dict[str, str],
) -> DatasetSpec:
    return DatasetSpec(
        dataset_id=dataset_id,
        module_id=module,
        clock_kind="scheduled_release",
        fact_family="market_position",
        adapter_id="cftc_tff",
        source_id="cftc",
        source_url="https://publicreporting.cftc.gov/resource/gpe5-46if.json",
        label=label,
        series_id=dataset_id.upper().replace(".", "_"),
        unit="percent_open_interest",
        frequency="weekly",
        freshness_seconds=_WEEKLY_FRESHNESS_SECONDS,
        refresh_seconds=21_600,
        trust_tier="official",
        critical=False,
        metadata={
            "official_owner": "U.S. Commodity Futures Trading Commission",
            "report": "Traders in Financial Futures - Futures Only",
            "contracts": contracts,
            "position_measure": "leveraged_money_net_pct_open_interest",
        },
    )


_DATASETS = (
    _fred("DGS2", module="rates_fed", label="2年期美国国债收益率", unit="percent", frequency="daily", critical=True),
    _fred("DGS10", module="rates_fed", label="10年期美国国债收益率", unit="percent", frequency="daily", critical=True),
    _fred("DGS30", module="rates_fed", label="30年期美国国债收益率", unit="percent", frequency="daily"),
    _fred("DFII10", module="rates_fed", label="10年期实际利率", unit="percent", frequency="daily"),
    _fred("T10YIE", module="rates_fed", label="10年期盈亏平衡通胀率", unit="percent", frequency="daily"),
    _fred("EFFR", module="rates_fed", label="有效联邦基金利率", unit="percent", frequency="daily", critical=True),
    _fred("DFEDTARU", module="rates_fed", label="联邦基金目标上限", unit="percent", frequency="daily"),
    _fred("DFEDTARL", module="rates_fed", label="联邦基金目标下限", unit="percent", frequency="daily"),
    _fred(
        "CPIAUCSL",
        module="economy_inflation",
        label="消费者价格指数",
        unit="index",
        frequency="monthly",
        clock="scheduled_release",
        freshness_seconds=_MONTHLY_FRESHNESS_SECONDS,
        critical=True,
    ),
    _fred(
        "CPILFESL",
        module="economy_inflation",
        label="核心消费者价格指数",
        unit="index",
        frequency="monthly",
        clock="scheduled_release",
        freshness_seconds=_MONTHLY_FRESHNESS_SECONDS,
    ),
    _fred(
        "PCEPI",
        module="economy_inflation",
        label="个人消费支出价格指数",
        unit="index",
        frequency="monthly",
        clock="scheduled_release",
        freshness_seconds=_MONTHLY_FRESHNESS_SECONDS,
        critical=True,
    ),
    _fred(
        "PCEPILFE",
        module="economy_inflation",
        label="核心个人消费支出价格指数",
        unit="index",
        frequency="monthly",
        clock="scheduled_release",
        freshness_seconds=_MONTHLY_FRESHNESS_SECONDS,
    ),
    _fred(
        "GDPC1",
        module="economy_inflation",
        label="实际国内生产总值",
        unit="billions_chained_2017_usd",
        frequency="quarterly",
        clock="scheduled_release",
        freshness_seconds=_QUARTERLY_FRESHNESS_SECONDS,
    ),
    _fred(
        "UNRATE",
        module="economy_inflation",
        label="失业率",
        unit="percent",
        frequency="monthly",
        clock="scheduled_release",
        freshness_seconds=_MONTHLY_FRESHNESS_SECONDS,
        critical=True,
    ),
    _fred(
        "PAYEMS",
        module="economy_inflation",
        label="非农就业总人数",
        unit="thousands_persons",
        frequency="monthly",
        clock="scheduled_release",
        freshness_seconds=_MONTHLY_FRESHNESS_SECONDS,
        critical=True,
    ),
    _fred(
        "ICSA",
        module="economy_inflation",
        label="首次申请失业救济人数",
        unit="persons",
        frequency="weekly",
        clock="scheduled_release",
        freshness_seconds=_WEEKLY_FRESHNESS_SECONDS,
    ),
    _fred(
        "RSAFS",
        module="economy_inflation",
        label="零售和餐饮销售",
        unit="millions_usd",
        frequency="monthly",
        clock="scheduled_release",
        freshness_seconds=_MONTHLY_FRESHNESS_SECONDS,
    ),
    _fred(
        "INDPRO",
        module="economy_inflation",
        label="工业生产指数",
        unit="index",
        frequency="monthly",
        clock="scheduled_release",
        freshness_seconds=_MONTHLY_FRESHNESS_SECONDS,
    ),
    _bls_release(
        "CUUR0000SA0",
        dataset_id="bls.cpi.release",
        label="BLS CPI官方发布事实",
        unit="index",
        importance_tier=3,
    ),
    _bls_release(
        "CUUR0000SA0L1E",
        dataset_id="bls.core_cpi.release",
        label="BLS核心CPI官方发布事实",
        unit="index",
        importance_tier=3,
    ),
    _bls_release(
        "LNS14000000",
        dataset_id="bls.unemployment.release",
        label="BLS失业率官方发布事实",
        unit="percent",
        importance_tier=3,
    ),
    _bls_release(
        "CES0000000001",
        dataset_id="bls.payrolls.release",
        label="BLS非农就业官方发布事实",
        unit="thousands_persons",
        importance_tier=3,
    ),
    _cftc_tff(
        "cftc.tff.rates_positions",
        module="rates_fed",
        label="CFTC利率期货杠杆资金净仓位",
        contracts={
            "042601": "UST 2Y",
            "043602": "UST 10Y",
            "020601": "UST BOND",
            "134741": "SOFR 3M",
        },
    ),
    _fred(
        "WALCL",
        module="liquidity_funding",
        label="美联储总资产",
        unit="millions_usd",
        frequency="weekly",
        critical=True,
    ),
    _fred(
        "WRBWFRBL",
        module="liquidity_funding",
        label="存款机构准备金余额",
        unit="millions_usd",
        frequency="weekly",
    ),
    _fred("RRPONTSYD", module="liquidity_funding", label="隔夜逆回购余额", unit="billions_usd", frequency="daily"),
    _fred("WTREGEN", module="liquidity_funding", label="美国财政部一般账户", unit="millions_usd", frequency="weekly"),
    _fred(
        "SOFR",
        module="liquidity_funding",
        label="担保隔夜融资利率",
        unit="percent",
        frequency="daily",
        critical=True,
    ),
    _fred("IORB", module="liquidity_funding", label="准备金余额利率", unit="percent", frequency="daily"),
    _fred(
        "BAMLC0A0CM",
        module="credit",
        label="美国投资级公司债期权调整利差",
        unit="percent",
        frequency="daily",
        critical=True,
    ),
    _fred(
        "BAMLH0A0HYM2",
        module="credit",
        label="美国高收益公司债期权调整利差",
        unit="percent",
        frequency="daily",
        critical=True,
    ),
    _fred("BAMLC0A4CBBB", module="credit", label="BBB级公司债期权调整利差", unit="percent", frequency="daily"),
    _fred("BAMLH0A3HYC", module="credit", label="CCC及以下高收益债利差", unit="percent", frequency="daily"),
    _fred("NFCI", module="credit", label="芝加哥联储金融状况指数", unit="index", frequency="weekly"),
    _fred(
        "DRTSCILM",
        module="credit",
        label="大型企业贷款标准净收紧比例",
        unit="percent",
        frequency="quarterly",
        freshness_seconds=_QUARTERLY_FRESHNESS_SECONDS,
    ),
    _cftc_tff(
        "cftc.tff.credit_positions",
        module="credit",
        label="CFTC信用期货杠杆资金净仓位",
        contracts={
            "221606": "Bloomberg IG Credit",
            "221605": "Bloomberg HY Credit",
        },
    ),
    _fred("VIXCLS", module="volatility", label="VIX现货指数", unit="index", frequency="daily", critical=True),
    _fred("VXVCLS", module="volatility", label="3个月VIX指数", unit="index", frequency="daily"),
    _fred("VXNCLS", module="volatility", label="纳斯达克100波动率指数", unit="index", frequency="daily"),
    _fred("GVZCLS", module="volatility", label="黄金ETF波动率指数", unit="index", frequency="daily"),
    _fred("OVXCLS", module="volatility", label="原油ETF波动率指数", unit="index", frequency="daily"),
    DatasetSpec(
        dataset_id="federal_reserve.monetary_policy.documents",
        module_id="rates_fed",
        clock_kind="official_document",
        fact_family="document",
        adapter_id="fed_rss",
        source_id="federal_reserve",
        source_url="https://www.federalreserve.gov/feeds/press_monetary.xml",
        label="美联储货币政策官方文件",
        series_id="FED_MONETARY_POLICY_DOCUMENTS",
        unit="document",
        frequency="event",
        freshness_seconds=7_776_000,
        refresh_seconds=3_600,
        critical=False,
        metadata={
            "official_owner": "Board of Governors of the Federal Reserve System",
            "role": "official_policy_document",
        },
    ),
    _nasdaq_history("SPY", "spy", label="SPDR标普500 ETF", asset_class="equity"),
    _nasdaq_history("TLT", "tlt", label="iShares 20年以上美国国债ETF", asset_class="rates"),
    _nasdaq_history("HYG", "hyg", label="iShares高收益公司债ETF", asset_class="credit"),
    _nasdaq_history("UUP", "dxy", label="美元指数代理（UUP）", asset_class="fx"),
    _nasdaq_history("GLD", "gld", label="SPDR黄金ETF", asset_class="commodity"),
    _nasdaq_history("USO", "uso", label="美国原油基金", asset_class="commodity"),
    DatasetSpec(
        dataset_id="binance.btcusdt.spot",
        module_id="cross_asset",
        clock_kind="daily_settlement",
        fact_family="market_observation",
        adapter_id="binance_spot",
        source_id="binance",
        source_url="https://api.binance.com/api/v3/klines",
        label="比特币 UTC 日线收盘",
        series_id="BTCUSDT",
        unit="usdt",
        frequency="daily",
        freshness_seconds=_DAILY_FRESHNESS_SECONDS,
        refresh_seconds=21_600,
        trust_tier="exchange",
        instrument_id="btc",
        symbol="BTC",
        instrument_name="Bitcoin",
        asset_class="crypto",
        instrument_type="spot",
        venue="binance",
    ),
    DatasetSpec(
        dataset_id="cboe.cfe.vx.settlement",
        module_id="cross_asset",
        clock_kind="daily_settlement",
        fact_family="market_settlement",
        adapter_id="cfe_settlement",
        source_id="cboe_cfe",
        source_url="https://www.cboe.com/us/futures/market_statistics/settlement/csv",
        label="CFE VIX期货官方结算",
        series_id="VX",
        unit="index_points",
        frequency="daily",
        freshness_seconds=259_200,
        refresh_seconds=21_600,
        trust_tier="official",
        instrument_id="vx_future",
        symbol="VX",
        instrument_name="Cboe VIX Futures",
        asset_class="volatility",
        instrument_type="future",
        venue="CFE",
        critical=True,
    ),
    _cftc_tff(
        "cftc.tff.cross_asset_positions",
        module="cross_asset",
        label="CFTC跨资产期货杠杆资金净仓位",
        contracts={
            "13874A": "E-mini S&P 500",
            "098662": "U.S. Dollar Index",
            "133741": "Bitcoin",
        },
    ),
    DatasetSpec(
        dataset_id="cme.rates.futures.curves",
        module_id="rates_fed",
        clock_kind="daily_settlement",
        fact_family="market_settlement",
        adapter_id="unavailable",
        source_id="cme_licensed",
        source_url="https://www.cmegroup.com/market-data.html",
        label="CME利率期货曲线",
        series_id="CME_RATES_CURVES",
        unit="price",
        frequency="intraday",
        freshness_seconds=86_400,
        refresh_seconds=86_400,
        trust_tier="official",
        critical=False,
        metadata={"unavailable_reason": "licensed_data_not_configured"},
    ),
)

DATASET_REGISTRY = MappingProxyType({spec.dataset_id: spec for spec in _DATASETS})

if len(DATASET_REGISTRY) != len(_DATASETS):
    raise RuntimeError("macro_dataset_registry_duplicate_id")


def datasets_for_clock(clock_kind: str) -> tuple[DatasetSpec, ...]:
    return tuple(spec for spec in _DATASETS if spec.clock_kind == clock_kind)


def datasets_for_module(module_id: MacroModuleId) -> tuple[DatasetSpec, ...]:
    return tuple(spec for spec in _DATASETS if spec.module_id == module_id)


def require_dataset(dataset_id: str) -> DatasetSpec:
    try:
        return DATASET_REGISTRY[str(dataset_id)]
    except KeyError as exc:
        raise ValueError(f"unknown_macro_dataset:{dataset_id}") from exc


__all__ = [
    "DATASET_REGISTRY",
    "datasets_for_clock",
    "datasets_for_module",
    "require_dataset",
]
