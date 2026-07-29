from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

from tracefold.macro.domain import MacroModuleId

CoverageRequirement = Literal["required", "supporting"]
CoverageState = Literal["complete", "partial"]


@dataclass(frozen=True, slots=True)
class CoverageSpec:
    capability_id: str
    module_id: MacroModuleId
    label: str
    requirement: CoverageRequirement
    dataset_ids: tuple[str, ...]


_COVERAGE = (
    CoverageSpec(
        "rates.nominal_curve",
        "rates_fed",
        "Treasury 1M–30Y 名义收益率曲线",
        "required",
        ("treasury.daily_nominal_curve",),
    ),
    CoverageSpec(
        "rates.real_curve",
        "rates_fed",
        "Treasury 5Y–30Y 实际收益率曲线",
        "required",
        ("treasury.daily_real_curve",),
    ),
    CoverageSpec(
        "rates.policy_corridor",
        "rates_fed",
        "EFFR、目标区间与 SOFR",
        "required",
        ("fred.effr", "fred.dfedtaru", "fred.dfedtarl", "fred.sofr"),
    ),
    CoverageSpec(
        "fed.fomc_materials",
        "rates_fed",
        "FOMC statement、minutes、SEP 与 implementation material",
        "required",
        ("federal_reserve.fomc.documents",),
    ),
    CoverageSpec(
        "fed.board_speeches",
        "rates_fed",
        "美联储理事政策相关讲话全文",
        "required",
        ("federal_reserve.board.speeches",),
    ),
    CoverageSpec(
        "fed.reserve_bank_speeches",
        "rates_fed",
        "联储银行行长政策相关讲话全文",
        "required",
        ("federal_reserve.reserve_bank.speeches",),
    ),
    CoverageSpec(
        "fed.roster",
        "rates_fed",
        "官员角色、FOMC 参与与投票状态",
        "required",
        ("federal_reserve.fomc.roster",),
    ),
    CoverageSpec(
        "fed.document_analysis",
        "rates_fed",
        "不可变、证据绑定的政策文件分析",
        "required",
        ("federal_reserve.document.analysis",),
    ),
    CoverageSpec(
        "economy.activity",
        "economy_inflation",
        "增长、消费与工业活动",
        "required",
        ("bea.gdp.release", "fred.gdpc1", "fred.rsafs", "fred.indpro"),
    ),
    CoverageSpec(
        "economy.inflation",
        "economy_inflation",
        "CPI 与 PCE 通胀",
        "required",
        (
            "bls.cpi.release",
            "bls.core_cpi.release",
            "bea.pce.release",
            "bea.core_pce.release",
            "fred.cpiaucsl",
            "fred.cpilfesl",
            "fred.pcepi",
            "fred.pcepilfe",
        ),
    ),
    CoverageSpec(
        "economy.labor",
        "economy_inflation",
        "就业、失业率与初请",
        "required",
        ("fred.payems", "fred.unrate", "fred.icsa"),
    ),
    CoverageSpec(
        "liquidity.balance_sheet",
        "liquidity_funding",
        "美联储资产、准备金、TGA 与 RRP",
        "required",
        ("fred.walcl", "fred.wrbwfrbl", "fred.wtregen", "fred.rrpontsyd"),
    ),
    CoverageSpec(
        "liquidity.funding",
        "liquidity_funding",
        "SOFR 与 IORB",
        "required",
        ("fred.sofr", "fred.iorb"),
    ),
    CoverageSpec(
        "credit.spread_ladder",
        "credit",
        "IG、BBB、BB、B、CCC OAS 评级梯级",
        "required",
        (
            "fred.bamlc0a0cm",
            "fred.bamlc0a4cbbb",
            "fred.bamlh0a1hybb",
            "fred.bamlh0a2hyb",
            "fred.bamlh0a3hyc",
        ),
    ),
    CoverageSpec(
        "credit.effective_yields",
        "credit",
        "IG 与 HY 绝对融资成本",
        "required",
        ("fred.bamlc0a0cmey", "fred.bamlh0a0hym2ey"),
    ),
    CoverageSpec(
        "credit.bank_supply",
        "credit",
        "C&I、CRE 与消费信贷供给和需求",
        "required",
        (
            "fred.drtscilm",
            "fred.drsdcilm",
            "fred.sublpdrcsn",
            "fred.sublpdrcdn",
            "fred.drtsclcc",
            "fred.demcc",
        ),
    ),
    CoverageSpec(
        "credit.loan_quality",
        "credit",
        "企业、CRE 与消费贷款逾期和核销",
        "required",
        (
            "fred.drblacbs",
            "fred.drcrelexfacbs",
            "fred.drcclacbs",
            "fred.corblacbs",
            "fred.corccacbs",
        ),
    ),
    CoverageSpec(
        "credit.market_confirmation",
        "credit",
        "LQD/HYG 与 CFTC 市场确认",
        "supporting",
        (
            "nasdaq.lqd.daily",
            "nasdaq.hyg.daily",
            "yfinance.lqd.intraday",
            "yfinance.hyg.intraday",
            "cftc.tff.credit_positions",
        ),
    ),
    CoverageSpec(
        "volatility.core",
        "volatility",
        "VIX 现货、期限与跨资产隐含波动率",
        "required",
        ("fred.vixcls", "fred.vxvcls", "fred.vxncls", "fred.gvzcls", "fred.ovxcls"),
    ),
    CoverageSpec(
        "volatility.official_vx_curve",
        "volatility",
        "带官方到期日的 CFE VIX 期货结算曲线",
        "required",
        ("cboe.cfe.vx.settlement",),
    ),
    CoverageSpec(
        "cross_asset.etf_matrix",
        "cross_asset",
        "固定十只 ETF 盘中代理矩阵",
        "required",
        (
            "yfinance.spy.intraday",
            "yfinance.qqq.intraday",
            "yfinance.iwm.intraday",
            "yfinance.tlt.intraday",
            "yfinance.ief.intraday",
            "yfinance.lqd.intraday",
            "yfinance.hyg.intraday",
            "yfinance.dxy.intraday",
            "yfinance.gld.intraday",
            "yfinance.uso.intraday",
        ),
    ),
    CoverageSpec(
        "cross_asset.etf_daily_history",
        "cross_asset",
        "固定十只 ETF 五年日线",
        "required",
        (
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
        ),
    ),
    CoverageSpec(
        "cross_asset.wti",
        "cross_asset",
        "WTI Cushing 官方日频现货",
        "required",
        ("fred.dcoilwtico",),
    ),
    CoverageSpec(
        "cross_asset.benchmarks",
        "cross_asset",
        "BTC、VIX 与市场基准",
        "required",
        (
            "yfinance.btc_yahoo.intraday",
            "yfinance.vix_index.intraday",
            "binance.btcusdt.spot",
            "fred.vixcls",
        ),
    ),
    CoverageSpec(
        "cross_asset.major_futures_market",
        "cross_asset",
        "股指、利率、商品主要期货与美元指数",
        "required",
        (
            "yfinance.es_future.intraday",
            "yfinance.nq_future.intraday",
            "yfinance.rty_future.intraday",
            "yfinance.zb_future.intraday",
            "yfinance.zn_future.intraday",
            "yfinance.dx_future.intraday",
            "yfinance.gc_future.intraday",
            "yfinance.cl_future.intraday",
            "yfinance.hg_future.intraday",
        ),
    ),
    CoverageSpec(
        "cross_asset.major_futures_daily_history",
        "cross_asset",
        "股指、利率、商品主要期货与美元指数五年日线",
        "required",
        (
            "yfinance.es_future.daily",
            "yfinance.nq_future.daily",
            "yfinance.rty_future.daily",
            "yfinance.zb_future.daily",
            "yfinance.zn_future.daily",
            "yfinance.dx_future.daily",
            "yfinance.gc_future.daily",
            "yfinance.cl_future.daily",
            "yfinance.hg_future.daily",
        ),
    ),
    CoverageSpec(
        "cross_asset.futures_confirmation",
        "cross_asset",
        "跨资产 CFTC 仓位",
        "supporting",
        ("cftc.tff.cross_asset_positions",),
    ),
)

COVERAGE_MANIFEST = MappingProxyType({spec.capability_id: spec for spec in _COVERAGE})

if len(COVERAGE_MANIFEST) != len(_COVERAGE):
    raise RuntimeError("macro_coverage_manifest_duplicate_capability")


def coverage_for_module(module_id: MacroModuleId) -> tuple[CoverageSpec, ...]:
    return tuple(spec for spec in _COVERAGE if spec.module_id == module_id)


__all__ = [
    "COVERAGE_MANIFEST",
    "CoverageRequirement",
    "CoverageSpec",
    "CoverageState",
    "coverage_for_module",
]
