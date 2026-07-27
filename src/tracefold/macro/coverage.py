from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

from tracefold.macro.domain import MacroModuleId

CoverageRequirement = Literal["required", "supporting", "licensed_unavailable"]
CoverageState = Literal["complete", "partial", "licensed_unavailable"]


@dataclass(frozen=True, slots=True)
class CoverageSpec:
    capability_id: str
    module_id: MacroModuleId
    label: str
    requirement: CoverageRequirement
    dataset_ids: tuple[str, ...]
    unavailable_reason: str | None = None


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
        "rates.cme_policy_futures",
        "rates_fed",
        "CME 政策利率期货概率",
        "licensed_unavailable",
        ("cme.rates.futures.curves",),
        "licensed_contract_facts_not_configured",
    ),
    CoverageSpec(
        "economy.activity",
        "economy_inflation",
        "增长、消费与工业活动",
        "required",
        ("fred.gdpc1", "fred.rsafs", "fred.indpro"),
    ),
    CoverageSpec(
        "economy.inflation",
        "economy_inflation",
        "CPI 与 PCE 通胀",
        "required",
        ("fred.cpiaucsl", "fred.cpilfesl", "fred.pcepi", "fred.pcepilfe"),
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
        ("nasdaq.lqd.history", "nasdaq.hyg.history", "cftc.tff.credit_positions"),
    ),
    CoverageSpec(
        "credit.trace_transactions",
        "credit",
        "TRACE 逐笔与 ETF NAV 溢折价",
        "licensed_unavailable",
        ("licensed.credit.trace_nav",),
        "licensed_security_level_facts_not_configured",
    ),
    CoverageSpec(
        "credit.ice_bofa_full_history",
        "credit",
        "ICE BofA 信用指数三年前完整历史",
        "licensed_unavailable",
        ("licensed.credit.ice_bofa_full_history",),
        "ice_bofa_history_before_public_three_year_window_unavailable",
    ),
    CoverageSpec(
        "volatility.core",
        "volatility",
        "VIX 现货、期限与跨资产隐含波动率",
        "required",
        ("fred.vixcls", "fred.vxvcls", "fred.vxncls", "fred.gvzcls", "fred.ovxcls"),
    ),
    CoverageSpec(
        "cross_asset.etf_matrix",
        "cross_asset",
        "固定十只 ETF 代理矩阵",
        "required",
        (
            "nasdaq.spy.history",
            "nasdaq.qqq.history",
            "nasdaq.iwm.history",
            "nasdaq.tlt.history",
            "nasdaq.ief.history",
            "nasdaq.lqd.history",
            "nasdaq.hyg.history",
            "nasdaq.dxy.history",
            "nasdaq.gld.history",
            "nasdaq.uso.history",
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
        ("binance.btcusdt.spot", "fred.vixcls"),
    ),
    CoverageSpec(
        "cross_asset.futures_confirmation",
        "cross_asset",
        "VIX 期货结算与跨资产 CFTC 仓位",
        "supporting",
        ("cboe.cfe.vx.settlement", "cftc.tff.cross_asset_positions"),
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
