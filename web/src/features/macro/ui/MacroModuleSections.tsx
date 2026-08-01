import { ExternalLink } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

import type {
  JsonObject,
  MacroCreditReadData,
  MacroEconomyInflationReadData,
  MacroLiquidityFundingReadData,
  MacroRatesFedReadData,
  MacroTypedModuleReadData,
  MacroVolatilityReadData,
} from "../model/macroTypes";
import {
  asRecord,
  asRecords,
  parseCorrelations,
  parseCrossAssetNormalizedGroups,
  parseCrossAssetReturnMatrix,
  parseCrossAssetSourceIdentity,
  parseCurveSnapshots,
  parseHistory,
  parseIndicators,
  parsePointRecords,
  parsePositions,
  parseReleases,
  readBoolean,
  readNumber,
  readRecord,
  readRecords,
  readText,
  type MacroCrossAssetNormalizedGroupView,
  type MacroCrossAssetReturnRowView,
  type MacroCrossAssetSourceIdentityView,
  type MacroCrossAssetSourceView,
  type MacroIndicatorView,
  type MacroPositionView,
} from "../model/macroViewModels";

import {
  MacroBarChart,
  MacroCorrelationHeatmap,
  MacroCurveChart,
  MacroIndicatorTable,
  MacroReleaseStrip,
  MacroTimeSeriesChart,
  type MacroBarGroup,
  type MacroChartAnnotation,
  type MacroChartSeries,
} from "./MacroCharts";

import "./MacroRatesDecision.css";
import "./MacroModuleSections.css";

type Section = { id: string; label: string };

const SECTIONS: Record<MacroTypedModuleReadData["module_id"], readonly Section[]> = {
  rates_fed: [
    { id: "curve", label: "收益率曲线" },
    { id: "policy", label: "政策走廊" },
    { id: "fed", label: "美联储沟通" },
    { id: "positioning", label: "利率仓位" },
  ],
  economy_inflation: [
    { id: "inflation", label: "通胀" },
    { id: "labor", label: "就业" },
    { id: "growth", label: "增长" },
  ],
  liquidity_funding: [
    { id: "balance-sheet", label: "资产负债表" },
    { id: "funding", label: "融资条件" },
  ],
  credit: [
    { id: "cycle", label: "周期四维" },
    { id: "spreads", label: "评级利差" },
    { id: "funding", label: "融资成本" },
    { id: "banks", label: "银行供需" },
    { id: "quality", label: "贷款质量" },
    { id: "confirmation", label: "市场确认" },
  ],
  volatility: [
    { id: "term", label: "现货–3M" },
    { id: "cross-asset", label: "跨资产隐波" },
  ],
  cross_asset: [
    { id: "returns", label: "收益矩阵" },
    { id: "normalized", label: "分组走势" },
    { id: "correlations", label: "相关矩阵" },
    { id: "futures", label: "期货与仓位" },
  ],
};

export function MacroModuleSections({ module }: { module: MacroTypedModuleReadData }) {
  const sections = SECTIONS[module.module_id];
  const active = useHashSection(sections);
  const panelId = `macro-${module.module_id}-${active}-panel`;
  return (
    <div className="macro-decision__module-workspace">
      <div className="macro-decision__section-navigation">
        <nav aria-label={`${module.label}内部视图`}>
          <div className="macro-decision__section-nav" role="tablist">
            {sections.map((section, index) => (
              <a
                aria-controls={`macro-${module.module_id}-${section.id}-panel`}
                aria-selected={active === section.id}
                href={`#${section.id}`}
                id={`macro-${module.module_id}-${section.id}-tab`}
                key={section.id}
                onKeyDown={(event) => {
                  const direction =
                    event.key === "ArrowRight" || event.key === "ArrowDown"
                      ? 1
                      : event.key === "ArrowLeft" || event.key === "ArrowUp"
                        ? -1
                        : 0;
                  const targetIndex =
                    event.key === "Home"
                      ? 0
                      : event.key === "End"
                        ? sections.length - 1
                        : direction
                          ? (index + direction + sections.length) % sections.length
                          : null;
                  if (targetIndex == null) return;
                  event.preventDefault();
                  const target = sections[targetIndex]!;
                  window.location.hash = target.id;
                  document.getElementById(`macro-${module.module_id}-${target.id}-tab`)?.focus();
                }}
                role="tab"
                tabIndex={active === section.id ? 0 : -1}
              >
                {section.label}
              </a>
            ))}
          </div>
        </nav>
        <label className="macro-decision__section-select">
          <span>当前视图</span>
          <select
            aria-label={`${module.label}当前视图`}
            onChange={(event) => {
              window.location.hash = event.target.value;
            }}
            value={active}
          >
            {sections.map((section) => (
              <option key={section.id} value={section.id}>
                {section.label}
              </option>
            ))}
          </select>
        </label>
      </div>
      <div
        aria-labelledby={`macro-${module.module_id}-${active}-tab`}
        id={panelId}
        role="tabpanel"
        tabIndex={0}
      >
        {module.module_id === "rates_fed" ? <RatesSection active={active} module={module} /> : null}
        {module.module_id === "economy_inflation" ? (
          <EconomySection active={active} module={module} />
        ) : null}
        {module.module_id === "liquidity_funding" ? (
          <LiquiditySection active={active} module={module} />
        ) : null}
        {module.module_id === "credit" ? <CreditSection active={active} module={module} /> : null}
        {module.module_id === "volatility" ? (
          <VolatilitySection active={active} module={module} />
        ) : null}
        {module.module_id === "cross_asset" ? (
          <CrossAssetSection active={active} module={module} />
        ) : null}
      </div>
    </div>
  );
}

function useHashSection(sections: readonly Section[]) {
  const fallback = sections[0]?.id ?? "";
  const read = useCallback(() => {
    const candidate = window.location.hash.replace(/^#/, "");
    return sections.some((section) => section.id === candidate) ? candidate : fallback;
  }, [fallback, sections]);
  const [active, setActive] = useState(read);
  useEffect(() => {
    const onHashChange = () => setActive(read());
    window.addEventListener("hashchange", onHashChange);
    onHashChange();
    return () => window.removeEventListener("hashchange", onHashChange);
  }, [read]);
  return active;
}

export function RatesDecisionSummary({ module }: { module: MacroRatesFedReadData }) {
  const decision = module.decision;
  const oneDayClassification = decision.classifications.find((item) => item.window === "1d");
  return (
    <section
      aria-label="最近完整交易日收益率决策摘要"
      className="macro-decision__rates-decision"
      data-state={decision.state}
    >
      <header>
        <div>
          <span>OFFICIAL COMPLETED SESSION · 1D PRIMARY</span>
          <h2>{decision.headline ?? "最近完整交易日无法形成对齐结论"}</h2>
          <p>
            {decision.session_completeness.reference_date
              ? `财政部同日曲线 · ${decision.session_completeness.reference_date}`
              : (decision.session_completeness.reason ?? "缺少可审计的同日财政部曲线。")}
          </p>
        </div>
        <div className="macro-decision__rates-badges">
          <span data-state={decision.session_completeness.state}>
            Session {ratesCompletenessLabel(decision.session_completeness.state)}
          </span>
          {oneDayClassification ? (
            <span data-state={oneDayClassification.state}>1D · {oneDayClassification.label}</span>
          ) : null}
        </div>
      </header>

      <div aria-label="2Y 10Y 30Y 收益率矩阵" className="macro-decision__rates-matrix" role="table">
        <div role="row">
          <span role="columnheader">期限</span>
          <span role="columnheader">当前</span>
          <span role="columnheader">1D</span>
          <span role="columnheader">1W</span>
          <span role="columnheader">MTD</span>
        </div>
        {decision.tenor_matrix.map((row) => {
          const oneDay = ratesWindow(row, "1d");
          const oneWeek = ratesWindow(row, "1w");
          const mtd = ratesWindow(row, "mtd");
          return (
            <div key={row.tenor} role="row">
              <strong role="cell">{row.tenor}</strong>
              <span role="cell">
                {row.current ? `${formatNumber(row.current.yield_pct)}%` : "—"}
                <small>{row.current?.reference_date ?? "缺少观测"}</small>
              </span>
              <RatesWindowCell window={oneDay} />
              <RatesWindowCell window={oneWeek} />
              <RatesWindowCell window={mtd} />
            </div>
          );
        })}
      </div>

      <div className="macro-decision__rates-second-line">
        <section aria-label="核心期限利差">
          <header>
            <span>CORE SPREADS</span>
            <strong>曲线斜率</strong>
          </header>
          {decision.spread_summary.map((spread) => (
            <article key={spread.spread_id}>
              <span>{spread.label}</span>
              <strong>{formatOptional(spread.value_bp, "basis_points")}</strong>
              <small>
                1D {formatSignedBp(spread.change_1d_bp)}
                {spread.prior_date ? ` · ${spread.prior_date}→${spread.current_date}` : ""}
              </small>
            </article>
          ))}
        </section>
        <section aria-label="名义收益率单日机械分解">
          <header>
            <span>MECHANICAL DECOMPOSITION</span>
            <strong>名义 = 实际 + Breakeven</strong>
          </header>
          {decision.decompositions.map((item) => (
            <article data-state={item.state} key={item.tenor}>
              <span>{item.tenor}</span>
              {item.state === "available" ? (
                <>
                  <strong>
                    名义 {formatSignedBp(item.nominal_change_bp)} = 实际{" "}
                    {formatSignedBp(item.real_change_bp)} + Breakeven{" "}
                    {formatSignedBp(item.breakeven_change_bp)}
                  </strong>
                  {item.assessment ? <small>{item.assessment}</small> : null}
                </>
              ) : (
                <small>{item.gap ?? "对应分解不可用。"}</small>
              )}
            </article>
          ))}
        </section>
      </div>

      <details className="macro-decision__rates-audit">
        <summary>查看窗口、期限与来源审计</summary>
        <p>
          当日判断只使用 U.S. Treasury 名义与实际曲线；FRED
          单期限序列仅作历史延伸和来源核对，不参与当日 headline。
        </p>
        {decision.tenor_matrix.map((row) => (
          <article key={row.tenor}>
            <strong>{row.tenor}</strong>
            <span>
              {row.current?.dataset_id ?? "treasury.daily_nominal_curve"} ·{" "}
              {row.current?.reference_date ?? "无当前观测"}
            </span>
            {row.current ? (
              <a href={row.current.source_url} rel="noreferrer" target="_blank">
                财政部原始来源 <ExternalLink aria-hidden="true" />
              </a>
            ) : null}
            <small>{row.current?.fact_id ?? "无事实引用"}</small>
            {row.windows.map((window) => (
              <small key={window.window}>
                {ratesWindowLabel(window.window)} · {window.baseline_date ?? "无基准"}→
                {window.current_date ?? "无当前"} · {formatSignedBp(window.change_bp)}
              </small>
            ))}
          </article>
        ))}
      </details>
    </section>
  );
}

function RatesWindowCell({
  window,
}: {
  window: MacroRatesFedReadData["decision"]["tenor_matrix"][number]["windows"][number] | undefined;
}) {
  return (
    <span data-direction={ratesDirection(window?.change_bp ?? null)} role="cell">
      {formatSignedBp(window?.change_bp ?? null)}
      <small>
        {window?.baseline_date ? `${window.baseline_date}→${window.current_date}` : "基准不足"}
      </small>
    </span>
  );
}

function RatesSection({ module, active }: { module: MacroRatesFedReadData; active: string }) {
  if (active === "policy") {
    const indicators = parseIndicators(module.policy_pricing.rates);
    return (
      <ModuleWorkbench
        description="EFFR、目标区间、SOFR 与 Treasury 短长端使用各自事实序列，不生成缺少底层合约的 FedWatch 近似概率。"
        eyebrow="POLICY CORRIDOR"
        module={module}
        title="政策走廊与当前市场定价"
      >
        <ModuleTimeSeriesChart
          description="相同百分比坐标；默认按数据自然频率截取。"
          module={module}
          series={indicatorSeries(indicators)}
          title="政策利率与市场利率"
          unit="percent"
        />
        <LatestMetricStrip indicators={indicators} />
        <MacroIndicatorTable indicators={indicators} />
      </ModuleWorkbench>
    );
  }
  if (active === "fed") {
    return <FedCommunication module={module} />;
  }
  if (active === "positioning") {
    const positions = parsePositions(module.positioning);
    return (
      <ModuleWorkbench
        description="CFTC 分账户净仓位只作市场确认；零值按真实零柱显示。"
        eyebrow="CFTC POSITIONING"
        module={module}
        title="利率期货仓位确认"
      >
        <MacroBarChart
          baseline={0}
          groups={positionGroups(positions)}
          title="净仓位占未平仓量"
          unit="percent_open_interest"
        />
        <PositionSourceList rows={positions} />
      </ModuleWorkbench>
    );
  }

  const curve = asRecord(module.curve);
  const nominal = parseCurveSnapshots(curve, "nominal_snapshots", "yield_pct");
  const real = parseCurveSnapshots(curve, "real_snapshots", "yield_pct");
  const breakeven = parseCurveSnapshots(curve, "breakeven_snapshots", "breakeven_pct");
  const spreadContainer = readRecord(curve, "spreads");
  const spreadSeries: MacroChartSeries[] = Object.entries(spreadContainer).map(
    ([id, value], index) => ({
      id,
      label: spreadLabel(id),
      color: chartColor(index),
      points: parsePointRecords(value, "value_bp"),
    }),
  );
  const oneDayClassification = module.decision.classifications.find((item) => item.window === "1d");
  return (
    <ModuleWorkbench
      description="横轴始终是统一期限；名义、实际与 breakeven 分图，避免双轴与历史 sparkline 冒充收益率曲线。"
      eyebrow="TREASURY CURVE"
      module={module}
      title={`${oneDayClassification?.label ?? "收益率曲线形态尚未解释"} · 1D 主时钟`}
    >
      <RatesCurveCharts
        breakeven={breakeven}
        module={module}
        nominal={nominal}
        real={real}
        spreadSeries={spreadSeries}
      />
    </ModuleWorkbench>
  );
}

function RatesCurveCharts({
  nominal,
  real,
  breakeven,
  spreadSeries,
  module,
}: {
  nominal: ReturnType<typeof parseCurveSnapshots>;
  real: ReturnType<typeof parseCurveSnapshots>;
  breakeven: ReturnType<typeof parseCurveSnapshots>;
  spreadSeries: MacroChartSeries[];
  module: MacroRatesFedReadData;
}) {
  const [backgroundWindows, setBackgroundWindows] = useState<Set<"1w" | "mtd" | "3m">>(
    () => new Set(),
  );
  const visible = new Set(["current", "previous", ...backgroundWindows]);
  const filter = (snapshots: ReturnType<typeof parseCurveSnapshots>) =>
    snapshots.filter((snapshot) => visible.has(snapshot.window));
  return (
    <div className="macro-decision__chart-stack">
      <fieldset className="macro-decision__curve-window-controls">
        <legend>曲线叠加窗口</legend>
        <span>当前与前一交易日固定显示</span>
        {(
          [
            ["1w", "1W"],
            ["mtd", "MTD"],
            ["3m", "3M"],
          ] as const
        ).map(([window, label]) => (
          <label key={window}>
            <input
              checked={backgroundWindows.has(window)}
              onChange={(event) => {
                setBackgroundWindows((current) => {
                  const next = new Set(current);
                  if (event.target.checked) next.add(window);
                  else next.delete(window);
                  return next;
                });
              }}
              type="checkbox"
            />
            {label}
          </label>
        ))}
      </fieldset>
      <MacroCurveChart snapshots={filter(nominal)} title="名义 Treasury 曲线" />
      <div className="macro-decision__chart-pair">
        <MacroCurveChart snapshots={filter(real)} title="实际 Treasury 曲线" />
        <MacroCurveChart snapshots={filter(breakeven)} title="Breakeven 通胀补偿" />
      </div>
      <ModuleTimeSeriesChart
        baseline={0}
        description="2s10s、10s30s、3m10s 与 5s30s，统一使用基点。"
        module={module}
        series={spreadSeries}
        title="关键期限利差"
        unit="basis_points"
      />
    </div>
  );
}

function FedCommunication({ module }: { module: MacroRatesFedReadData }) {
  const fed = asRecord(module.fed);
  const institutional = readRecord(fed, "institutional_stance");
  const distribution = readRecord(fed, "officials_distribution");
  const eventCounts = readRecord(distribution, "stance_event_counts");
  const officialCounts = readRecord(distribution, "stance_unique_official_counts");
  const institutionalDirection = readText(institutional, "direction");
  const institutionalReason = readText(institutional, "reason");
  const institutionalAnalysisId = readText(institutional, "analysis_id");
  const stances = ["hawkish", "neutral", "dovish", "mixed"] as const;
  const groups: MacroBarGroup[] = stances.map((stance) => ({
    id: stance,
    label: stanceLabel(stance),
    values: [
      {
        id: "events",
        label: "事件数",
        value: readNumber(eventCounts, stance),
        color: chartColor(0),
      },
      {
        id: "officials",
        label: "独立官员数",
        value: readNumber(officialCounts, stance),
        color: chartColor(1),
      },
    ],
  }));
  return (
    <ModuleWorkbench
      description="机构声明与官员讲话是两条独立证据轨；分布来自已审阅文件，不给官员贴永久鹰鸽标签。"
      eyebrow="FED COMMUNICATION"
      module={module}
      title="制度立场、官员分布与事件时间线"
    >
      {institutionalDirection || institutionalReason || institutionalAnalysisId ? (
        <div className="macro-decision__fed-callout">
          <span>机构立场</span>
          {institutionalDirection ? <strong>{stanceLabel(institutionalDirection)}</strong> : null}
          {institutionalReason ? <p>{institutionalReason}</p> : null}
          {institutionalAnalysisId ? (
            <details>
              <summary>查看分析审计</summary>
              <small>{institutionalAnalysisId}</small>
            </details>
          ) : null}
        </div>
      ) : null}
      <MacroBarChart groups={groups} title="近 90 日已审阅沟通分布" />
      <FedTimeline rows={readRecords(fed, "timeline")} />
    </ModuleWorkbench>
  );
}

function EconomySection({
  module,
  active,
}: {
  module: MacroEconomyInflationReadData;
  active: string;
}) {
  const payload =
    active === "labor" ? module.labor : active === "growth" ? module.growth : module.inflation;
  const indicators = parseIndicators(payload);
  const releases = parseReleases(payload);
  const copy = {
    growth: {
      title: "增长与实际活动",
      description: "GDP、零售销售与工业生产各按自身发布频率呈现，不使用双轴拼接。",
    },
    inflation: {
      title: "通胀脉冲与黏性",
      description: "CPI、核心 CPI、PCE 与核心 PCE 分为同口径小图，并保留官方预期差与前值修订。",
    },
    labor: {
      title: "就业、失业率与初请",
      description: "月度就业、失业率与周度初请使用各自自然窗口，不把发布频率混成一条总分。",
    },
  }[active === "labor" || active === "growth" ? active : "inflation"];
  return (
    <ModuleWorkbench
      description={copy.description}
      eyebrow="ECONOMY & INFLATION"
      module={module}
      title={copy.title}
    >
      <IndicatorSmallMultiples indicators={indicators} module={module} />
      <MacroReleaseStrip releases={releases} />
      <MacroIndicatorTable indicators={indicators} />
    </ModuleWorkbench>
  );
}

function LiquiditySection({
  module,
  active,
}: {
  module: MacroLiquidityFundingReadData;
  active: string;
}) {
  if (active === "funding") {
    const funding = asRecord(module.funding);
    const indicators = parseIndicators(funding);
    const spread = parsePointRecords(funding.sofr_minus_iorb_bp_history, "value");
    return (
      <ModuleWorkbench
        description="SOFR 与 IORB 使用同一百分比坐标；SOFR−IORB 利差由服务端确定性计算，不在浏览器推导。"
        eyebrow="FUNDING CONDITIONS"
        module={module}
        title="隔夜融资价格与政策利率"
      >
        <ModuleTimeSeriesChart
          module={module}
          series={indicatorSeries(indicators)}
          title="SOFR 与 IORB"
          unit="percent"
        />
        <ModuleTimeSeriesChart
          baseline={0}
          module={module}
          series={[
            {
              id: "sofr-iorb",
              label: "SOFR − IORB",
              points: spread,
            },
          ]}
          title="SOFR−IORB 利差"
          unit="basis_points"
        />
        <MacroIndicatorTable indicators={indicators} />
      </ModuleWorkbench>
    );
  }
  const indicators = parseIndicators(module.balance_sheet);
  return (
    <ModuleWorkbench
      description="Fed 总资产、准备金、TGA 与 RRP 四条余额分图呈现，同时比较 1W / 1M 变化；不压成综合流动性分数。"
      eyebrow="BALANCE SHEET"
      module={module}
      title="资产负债表与储备流动性"
    >
      <IndicatorSmallMultiples indicators={indicators} module={module} />
      <MacroBarChart
        groups={indicatorChangeGroups(indicators)}
        title="余额变化"
        unit={commonUnit(indicators)}
      />
      <MacroIndicatorTable indicators={indicators} />
    </ModuleWorkbench>
  );
}

function CreditSection({ module, active }: { module: MacroCreditReadData; active: string }) {
  if (active === "cycle") {
    return (
      <ModuleWorkbench
        description="四个维度独立呈现水平、速度、供需与已实现质量；不生成综合信用分数。"
        eyebrow="CREDIT CYCLE"
        module={module}
        title="信用周期四维判断"
      >
        <CreditDimensions rows={module.cycle_dimensions} />
      </ModuleWorkbench>
    );
  }
  if (active === "spreads") {
    const ladder = asRecord(module.spread_ladder);
    const indicators = parseIndicators(readRecords(ladder, "rows"));
    const tailGap = readNumber(ladder, "tail_gap");
    return (
      <ModuleWorkbench
        description="IG → BBB → BB → B → CCC 同时展示当前水平、历史变化与真实样本分位。"
        eyebrow="SPREAD LADDER"
        module={module}
        title="评级利差梯级与尾部差"
      >
        <MacroBarChart
          baseline={0}
          groups={indicators.map((item) => ({
            id: item.datasetId,
            label: shortIndicatorLabel(item.label),
            values: [{ id: "level", label: "当前利差", value: item.latestValue }],
          }))}
          title="当前评级利差"
          unit={commonUnit(indicators)}
        />
        <ModuleTimeSeriesChart
          module={module}
          series={indicatorSeries(indicators)}
          title="评级利差历史"
          unit={commonUnit(indicators)}
        />
        {tailGap == null ? null : (
          <p className="macro-decision__callout">
            CCC−BB 尾差：
            {formatOptional(tailGap, readText(ladder, "tail_gap_unit"))}
          </p>
        )}
        <MacroIndicatorTable indicators={indicators} />
      </ModuleWorkbench>
    );
  }
  if (active === "funding") {
    const funding = asRecord(module.funding_costs);
    const corporate = parseIndicators(readRecords(funding, "corporate_yields"));
    const reference = parseIndicators(readRecords(funding, "reference_rates"));
    const comparisons = readRecords(funding, "comparisons");
    return (
      <ModuleWorkbench
        description="公司债 effective yield 与 EFFR、10Y Treasury 并列；紧利差不等于低融资成本。"
        eyebrow="ALL-IN FUNDING COST"
        module={module}
        title="公司债绝对融资成本"
      >
        <ModuleTimeSeriesChart
          module={module}
          series={indicatorSeries([...corporate, ...reference])}
          title="公司债与参考利率"
          unit="percent"
        />
        <MacroBarChart
          groups={comparisons.map((row, index) => ({
            id: readText(row, "label") ?? `comparison-${index}`,
            label: readText(row, "label") ?? `比较 ${index + 1}`,
            values: [
              {
                id: "spread",
                label: "利差",
                value: readNumber(row, "value_bp"),
              },
            ],
          }))}
          title="公司债收益率减参考利率"
          unit="basis_points"
        />
        <MacroIndicatorTable indicators={[...corporate, ...reference]} />
      </ModuleWorkbench>
    );
  }
  if (active === "banks") {
    const indicators = parseIndicators(module.bank_lending);
    return (
      <ModuleWorkbench
        description="C&I、CRE 与消费信贷的标准和需求按官方季度时钟分开读。"
        eyebrow="BANK SUPPLY & DEMAND"
        module={module}
        title="银行信贷供给与需求"
      >
        <IndicatorSmallMultiples indicators={indicators} module={module} />
        <MacroIndicatorTable indicators={indicators} />
      </ModuleWorkbench>
    );
  }
  if (active === "quality") {
    const indicators = parseIndicators(module.loan_quality);
    return (
      <ModuleWorkbench
        description="企业、CRE 与消费贷款逾期率和核销率检验市场信用定价。"
        eyebrow="REALIZED QUALITY"
        module={module}
        title="已实现贷款质量"
      >
        <IndicatorSmallMultiples indicators={indicators} module={module} />
        <MacroIndicatorTable indicators={indicators} />
      </ModuleWorkbench>
    );
  }
  const confirmations = asRecord(module.confirmations);
  const returnMatrix = parseCrossAssetReturnMatrix(confirmations.return_matrix);
  const sourceIdentity = parseCrossAssetSourceIdentity(confirmations.source_identity);
  const positions = parsePositions(readRecords(confirmations, "positions"));
  return (
    <ModuleWorkbench
      description="LQD/HYG 与 CFTC 只作确认；最新价和历史收益严格读取服务端指定来源，不在浏览器内替补。"
      eyebrow="MARKET CONFIRMATION"
      module={module}
      title="信用 ETF 与仓位确认"
    >
      <CrossAssetReturnMatrix label="信用 ETF 收益矩阵" rows={returnMatrix} />
      <CrossAssetSourceIdentityTable rows={sourceIdentity} />
      <MacroBarChart
        groups={positionGroups(positions)}
        title="信用期货净仓位"
        unit="percent_open_interest"
      />
    </ModuleWorkbench>
  );
}

function VolatilitySection({
  module,
  active,
}: {
  module: MacroVolatilityReadData;
  active: string;
}) {
  if (active === "cross-asset") {
    const payload = asRecord(module.cross_asset_implied);
    const indicators = parseIndicators(payload);
    const groups = parseCrossAssetNormalizedGroups(payload.normalized_groups);
    return (
      <ModuleWorkbench
        description="VXN、GVZ 与 OVX 先按各自水平呈现；服务端归一化序列用于跨资产方向比较。"
        eyebrow="CROSS-ASSET IMPLIED VOL"
        module={module}
        title="股票、黄金与原油隐含波动率"
      >
        {groups.length ? (
          <div className="macro-decision__chart-stack">
            {groups.map((group) => (
              <ModuleTimeSeriesChart
                baseline={100}
                key={group.groupId}
                module={module}
                series={crossAssetNormalizedSeries(group)}
                title={group.label}
                unit="index"
              />
            ))}
          </div>
        ) : (
          <IndicatorSmallMultiples indicators={indicators} module={module} />
        )}
        <MacroIndicatorTable indicators={indicators} />
      </ModuleWorkbench>
    );
  }
  const term = asRecord(module.term_structure);
  const indicators = parseIndicators(readRecords(term, "spot_and_three_month"));
  const spread = parseHistory(term.spread_history);
  const officialVxCurve = readRecords(term, "official_vx_curve");
  return (
    <ModuleWorkbench
      description="现货与三个月 VIX 用于时间序列比较；完整期货曲线只读取带官方到期日的 CFE 结算，不用合约代码猜到期。"
      eyebrow="VOLATILITY TERM STRUCTURE"
      module={module}
      title="VIX 期限结构"
    >
      <ModuleTimeSeriesChart
        module={module}
        series={indicatorSeries(indicators)}
        title="VIX 与 3M VIX"
        unit="index"
      />
      <ModuleTimeSeriesChart
        baseline={0}
        module={module}
        series={[{ id: "vix-vxv", label: "VIX − 3M VIX", points: spread }]}
        title="现货减三个月利差"
        unit="index_points"
      />
      <MacroIndicatorTable indicators={indicators} />
      <SettlementTable rows={officialVxCurve} />
    </ModuleWorkbench>
  );
}

function CrossAssetSection({
  module,
  active,
}: {
  module: Extract<MacroTypedModuleReadData, { module_id: "cross_asset" }>;
  active: string;
}) {
  const assetsPayload = asRecord(module.assets);
  if (active === "normalized") {
    const groups = parseCrossAssetNormalizedGroups(assetsPayload.normalized_groups);
    return (
      <ModuleWorkbench
        description="各资产由服务端归一为窗口首日 100，并按服务端返回的稳定分组和顺序拆图；不同价格单位不会直接比较。"
        eyebrow="NORMALIZED GROUPS"
        module={module}
        title="大类资产分组归一化走势"
      >
        {groups.length ? (
          <div className="macro-decision__chart-stack">
            {groups.map((group) => (
              <ModuleTimeSeriesChart
                baseline={100}
                key={group.groupId}
                module={module}
                series={crossAssetNormalizedSeries(group)}
                title={group.label}
                unit="index"
              />
            ))}
          </div>
        ) : (
          <EmptyState text="服务端尚未返回归一化分组。" />
        )}
      </ModuleWorkbench>
    );
  }
  if (active === "correlations") {
    return (
      <ModuleWorkbench
        description="相关性使用共同日收益样本，样本数随单元格保留；矩阵仅重排 API 事实。"
        eyebrow="CORRELATION MATRIX"
        module={module}
        title="跨资产相关性"
      >
        <MacroCorrelationHeatmap
          rows={parseCorrelations(module.correlations)}
          title="日收益相关矩阵"
        />
      </ModuleWorkbench>
    );
  }
  if (active === "futures") {
    const futures = asRecord(module.futures);
    const returnMatrix = parseCrossAssetReturnMatrix(futures.return_matrix);
    const positions = parsePositions(readRecords(futures, "positions"));
    return (
      <ModuleWorkbench
        description="连续期货市场与 CFTC 周度仓位分层呈现，不混用来源时钟；VIX 官方期限曲线由波动率模块负责。"
        eyebrow="FUTURES CONFIRMATION"
        module={module}
        title="期货市场与仓位确认"
      >
        <CrossAssetReturnMatrix label="期货收益矩阵" rows={returnMatrix} />
        <CrossAssetReturnSourceTable rows={returnMatrix} />
        <MacroBarChart
          groups={positionGroups(positions)}
          title="跨资产 CFTC 净仓位"
          unit="percent_open_interest"
        />
      </ModuleWorkbench>
    );
  }
  const returnMatrix = parseCrossAssetReturnMatrix(assetsPayload.return_matrix);
  const sourceIdentity = parseCrossAssetSourceIdentity(assetsPayload.source_identity);
  return (
    <ModuleWorkbench
      description="固定十只 ETF 用最新价与 1D / 1W / 1M 收益热力矩阵开场；每个单元格只读取服务端指定来源，不在浏览器中替补或混合。"
      eyebrow="CROSS-ASSET RETURNS"
      module={module}
      title="固定十只 ETF 收益矩阵"
    >
      <CrossAssetReturnMatrix label="十只 ETF 收益矩阵" rows={returnMatrix} />
      <CrossAssetSourceIdentityTable rows={sourceIdentity} />
    </ModuleWorkbench>
  );
}

function ModuleWorkbench({
  module,
  eyebrow,
  title,
  description,
  children,
}: {
  module: MacroTypedModuleReadData;
  eyebrow: string;
  title: string;
  description: string;
  children: React.ReactNode;
}) {
  return (
    <section className="macro-decision__semantic-section">
      <header className="macro-decision__section-heading">
        <span>{eyebrow}</span>
        <h2>{title}</h2>
        <p>{description}</p>
      </header>
      <div className="macro-decision__visual-workbench">
        <div className="macro-decision__chart-stage">{children}</div>
        <DecisionAnnotationRail module={module} />
      </div>
    </section>
  );
}

function DecisionAnnotationRail({ module }: { module: MacroTypedModuleReadData }) {
  if (module.module_id === "rates_fed") {
    return <RatesDecisionAnnotationRail module={module} />;
  }
  const summaryInterpretation = module.summary.interpretation;
  return (
    <aside aria-label="图表决策注释" className="macro-decision__annotation-rail">
      <header>
        <span>DECISION ANNOTATIONS</span>
        <strong>只展示当前 API 返回的判断与检查项</strong>
      </header>
      <article>
        <span>事实截点</span>
        <strong>{formatInstant(module.latest_fact_at_ms)}</strong>
        {module.summary.headline ? <small>{module.summary.headline}</small> : null}
        {summaryInterpretation ? <small>{summaryInterpretation}</small> : null}
      </article>
      {module.summary.top_changes.length ? (
        <article data-state="change">
          <span>关键变化</span>
          <ul>
            {module.summary.top_changes.map((change) => (
              <li key={`${change.dataset_id}:${change.as_of ?? change.importance_rank}`}>
                <strong>{change.label}</strong>
                <small>
                  {formatOptional(change.value, change.unit)}
                  {change.as_of ? ` · ${change.as_of}` : ""}
                </small>
                {change.importance_explanation ? (
                  <small>{change.importance_explanation}</small>
                ) : null}
                <details>
                  <summary>查看来源审计</summary>
                  <small>{change.dataset_id}</small>
                  {change.source_url ? (
                    <a href={change.source_url} rel="noreferrer" target="_blank">
                      原始来源 <ExternalLink aria-hidden="true" />
                    </a>
                  ) : null}
                </details>
              </li>
            ))}
          </ul>
        </article>
      ) : null}
      {module.contradictions.length ? (
        <article data-state="weakening">
          <span>矛盾</span>
          <ul>
            {module.contradictions.map((contradiction) => (
              <li key={contradiction}>
                <strong>{contradiction}</strong>
              </li>
            ))}
          </ul>
        </article>
      ) : null}
      {module.falsifiers.length ? (
        <article data-state="invalidated">
          <span>失效条件</span>
          <ul>
            {module.falsifiers.map((falsifier) => (
              <li key={falsifier}>
                <strong>{falsifier}</strong>
              </li>
            ))}
          </ul>
        </article>
      ) : null}
      {module.next_checkpoints.length ? (
        <article>
          <span>下一检查点</span>
          <ul>
            {module.next_checkpoints.map((checkpoint) => (
              <li key={`${checkpoint.dataset_id}:${checkpoint.label}`}>
                <strong>{checkpoint.label}</strong>
                <small>
                  当前数据 {checkpointCurrentLabel(checkpoint.current_health)} · 历史深度{" "}
                  {checkpointHistoryLabel(checkpoint.history_depth)}
                </small>
                {checkpoint.next_check_at_ms ? (
                  <time>下次检查 {formatInstant(checkpoint.next_check_at_ms)}</time>
                ) : null}
                {checkpoint.reason ? <small>{checkpoint.reason.message}</small> : null}
                {checkpoint.reason?.next_action ? (
                  <small>{checkpoint.reason.next_action}</small>
                ) : null}
                <details>
                  <summary>查看数据审计</summary>
                  <small>{checkpoint.dataset_id}</small>
                </details>
              </li>
            ))}
          </ul>
        </article>
      ) : null}
    </aside>
  );
}

function RatesDecisionAnnotationRail({ module }: { module: MacroRatesFedReadData }) {
  const decision = module.decision;
  const classification = decision.classifications.find((item) => item.window === "1d");
  return (
    <aside aria-label="图表决策注释" className="macro-decision__annotation-rail">
      <header>
        <span>1D DECISION AUDIT</span>
        <strong>期限、窗口和来源均由持久化合同提供</strong>
      </header>
      <article data-state={decision.state === "available" ? "change" : "weakening"}>
        <span>最近完整交易日</span>
        <strong>{decision.reference_date ?? "未对齐"}</strong>
        <small>{decision.headline ?? decision.session_completeness.reason}</small>
      </article>
      <article>
        <span>1D 曲线分类</span>
        <strong>{classification?.label ?? "不可用"}</strong>
        {classification?.inputs.prior_as_of ? (
          <small>
            {classification.inputs.prior_as_of}→{classification.inputs.current_as_of}
          </small>
        ) : null}
      </article>
      <article>
        <span>来源权威</span>
        <strong>U.S. Treasury · decision primary</strong>
        <small>FRED · history / reconciliation only</small>
      </article>
    </aside>
  );
}

type ModuleTimeSeriesProps = Parameters<typeof MacroTimeSeriesChart>[0] & {
  module: MacroTypedModuleReadData;
};

function ModuleTimeSeriesChart({ module, ...props }: ModuleTimeSeriesProps) {
  return (
    <MacroTimeSeriesChart
      {...props}
      annotations={[...(props.annotations ?? []), ...moduleChartAnnotations(module, props.series)]}
    />
  );
}

function IndicatorSmallMultiples({
  indicators,
  module,
}: {
  indicators: MacroIndicatorView[];
  module: MacroTypedModuleReadData;
}) {
  if (!indicators.length) return <EmptyState text="当前没有可用指标。" />;
  return (
    <div className="macro-decision__small-multiples">
      {indicators.map((indicator) => (
        <ModuleTimeSeriesChart
          key={indicator.datasetId}
          module={module}
          series={[
            {
              id: indicator.datasetId,
              label: indicator.label,
              points: indicator.history,
            },
          ]}
          title={indicator.label}
          unit={indicator.unit}
        />
      ))}
    </div>
  );
}

function LatestMetricStrip({ indicators }: { indicators: MacroIndicatorView[] }) {
  if (!indicators.length) return null;
  return (
    <div className="macro-decision__metric-strip">
      {indicators.map((indicator) => (
        <article key={indicator.datasetId}>
          <span>{indicator.label}</span>
          <strong>{formatOptional(indicator.latestValue, indicator.unit)}</strong>
          <small>
            1W {formatSigned(indicator.change1w)} · 1M {formatSigned(indicator.change1m)}
          </small>
        </article>
      ))}
    </div>
  );
}

function CreditDimensions({ rows }: { rows: JsonObject[] }) {
  const contentRows = rows.filter(
    (row) =>
      readText(row, "label") ||
      readText(row, "state") ||
      readText(row, "driver") ||
      asRecords(row.conflicts).length ||
      (Array.isArray(row.conflicts) && row.conflicts.length),
  );
  if (!contentRows.length) return <EmptyState text="信用周期维度尚未形成。" />;
  return (
    <div aria-label="信用周期四维结论" className="macro-decision__credit-dimensions">
      {contentRows.map((row, index) => {
        const conflicts = asRecords(row.conflicts).length
          ? asRecords(row.conflicts)
              .map((item) => readText(item, "text"))
              .filter(Boolean)
          : Array.isArray(row.conflicts)
            ? row.conflicts.filter((item): item is string => typeof item === "string")
            : [];
        return (
          <article
            data-state={readText(row, "state") ?? undefined}
            key={readText(row, "dimension_id") ?? index}
          >
            {readText(row, "label") ? <span>{readText(row, "label")}</span> : null}
            {readText(row, "state") ? (
              <strong>{creditStateLabel(readText(row, "state")!)}</strong>
            ) : null}
            {readText(row, "driver") ? <p>{readText(row, "driver")}</p> : null}
            {conflicts.map((conflict) => (
              <small key={conflict}>{conflict}</small>
            ))}
          </article>
        );
      })}
    </div>
  );
}

function checkpointCurrentLabel(value: string): string {
  return (
    {
      current: "当前",
      degraded: "降级",
      unavailable: "不可用",
    }[value] ?? "状态未解释"
  );
}

function checkpointHistoryLabel(value: string): string {
  return (
    {
      complete: "完整",
      insufficient: "不足",
      not_required: "不要求",
      partial: "部分",
    }[value] ?? "状态未解释"
  );
}

function creditStateLabel(value: string): string {
  return (
    {
      cheap: "融资成本偏低",
      deteriorating: "正在恶化",
      easing: "正在宽松",
      expensive: "融资成本偏高",
      improving: "正在改善",
      insufficient: "证据不足",
      neutral: "中性",
      normal: "常态",
      restrictive: "供给偏紧",
      stable: "稳定",
      stressed: "承压",
      tightening: "正在收紧",
      weak_demand: "需求偏弱",
    }[value] ?? "状态未解释"
  );
}

function CrossAssetReturnMatrix({
  rows,
  label,
}: {
  rows: MacroCrossAssetReturnRowView[];
  label: string;
}) {
  if (!rows.length) return <EmptyState text="服务端尚未返回资产收益矩阵。" />;
  return (
    <div aria-label={label} className="macro-decision__return-matrix" role="table">
      <div role="row">
        <span role="columnheader">资产</span>
        <span role="columnheader">最新</span>
        <span role="columnheader">1D</span>
        <span role="columnheader">1W</span>
        <span role="columnheader">1M</span>
      </div>
      {rows.map((row) => (
        <div
          data-display-order={row.displayOrder}
          data-group-id={row.groupId}
          key={`${row.displayOrder}:${row.symbol}`}
          role="row"
        >
          <span role="cell">
            <strong>{row.symbol}</strong>
            <small>{row.label}</small>
            <small>{row.groupLabel}</small>
          </span>
          <span role="cell">{formatOptional(row.latestSource.fact?.latestValue ?? null, "")}</span>
          <HeatCell value={row.returnSource.fact?.change1dPct ?? null} />
          <HeatCell value={row.returnSource.fact?.change1wPct ?? null} />
          <HeatCell value={row.returnSource.fact?.change1mPct ?? null} />
        </div>
      ))}
    </div>
  );
}

function CrossAssetSourceIdentityTable({ rows }: { rows: MacroCrossAssetSourceIdentityView[] }) {
  if (!rows.length) return null;
  return (
    <details className="macro-decision__source-table">
      <summary>查看来源身份与时钟（{rows.length}）</summary>
      <div role="table">
        <div role="row">
          <span role="columnheader">资产</span>
          <span role="columnheader">证据身份</span>
          <span role="columnheader">精确来源</span>
          <span role="columnheader">选择规则</span>
          <span role="columnheader">身份规则</span>
        </div>
        {rows.map((row) => (
          <div
            data-display-order={row.displayOrder}
            key={`${row.displayOrder}:${row.symbol}`}
            role="row"
          >
            <span role="cell">
              <strong>{row.symbol}</strong>
              <small>{row.label}</small>
            </span>
            <span role="cell">{row.evidenceKind}</span>
            <CrossAssetSourceList sources={row.sources} />
            <span role="cell">{row.selectionPolicy}</span>
            <span role="cell">{row.identityPolicy}</span>
          </div>
        ))}
      </div>
    </details>
  );
}

function CrossAssetReturnSourceTable({ rows }: { rows: MacroCrossAssetReturnRowView[] }) {
  if (!rows.length) return null;
  return (
    <details className="macro-decision__source-table">
      <summary>查看收益矩阵精确来源（{rows.length}）</summary>
      <div role="table">
        <div role="row">
          <span role="columnheader">资产</span>
          <span role="columnheader">最新价来源</span>
          <span role="columnheader">收益来源</span>
          <span role="columnheader">选择规则</span>
          <span role="columnheader">身份规则</span>
        </div>
        {rows.map((row) => (
          <div
            data-display-order={row.displayOrder}
            key={`${row.displayOrder}:${row.symbol}`}
            role="row"
          >
            <span role="cell">
              <strong>{row.symbol}</strong>
              <small>{row.label}</small>
            </span>
            <CrossAssetSourceCell source={row.latestSource} />
            <CrossAssetSourceCell source={row.returnSource} />
            <span role="cell">{row.selectionPolicy}</span>
            <span role="cell">{row.identityPolicy}</span>
          </div>
        ))}
      </div>
    </details>
  );
}

function CrossAssetSourceList({ sources }: { sources: MacroCrossAssetSourceView[] }) {
  return (
    <span role="cell">
      {sources.map((source) => (
        <span key={`${source.sourceRole}:${source.datasetId}`}>
          <strong>{source.label}</strong>
          <small>
            {source.datasetId} · {source.sourceRole}
          </small>
          <SourceClockAndLink source={source} />
        </span>
      ))}
    </span>
  );
}

function CrossAssetSourceCell({ source }: { source: MacroCrossAssetSourceView }) {
  return (
    <span role="cell">
      <strong>{source.label}</strong>
      <small>
        {source.datasetId} · {source.sourceRole}
      </small>
      <SourceClockAndLink source={source} />
    </span>
  );
}

function SourceClockAndLink({ source }: { source: MacroCrossAssetSourceView }) {
  const fact = source.fact;
  if (!fact) return <small>尚无来源事实</small>;
  return (
    <>
      <small>
        {fact.asOf ?? (fact.marketTimeMs ? formatInstant(fact.marketTimeMs) : "时钟未返回")}
      </small>
      {fact.sourceUrl ? (
        <a href={fact.sourceUrl} rel="noreferrer" target="_blank">
          来源 <ExternalLink aria-hidden="true" />
        </a>
      ) : null}
    </>
  );
}

function HeatCell({ value }: { value: number | null }) {
  const intensity = value == null ? 0 : Math.min(Math.abs(value) / 8, 1);
  return (
    <span
      data-direction={value == null ? "none" : value > 0 ? "up" : value < 0 ? "down" : "flat"}
      role="cell"
      style={{ "--heat-intensity": Math.max(intensity, 0.08) } as React.CSSProperties}
    >
      {value == null ? "—" : `${formatSigned(value)}%`}
    </span>
  );
}

function FedTimeline({ rows }: { rows: JsonObject[] }) {
  if (!rows.length) return <EmptyState text="FOMC 文件与讲话正文尚未回填。" />;
  return (
    <details className="macro-decision__timeline" open>
      <summary>政策事件时间线（最近 {Math.min(rows.length, 20)} 条）</summary>
      <ol>
        {rows.slice(0, 20).map((row, index) => {
          const analysis = readRecord(row, "analysis");
          return (
            <li key={readText(row, "document_id") ?? index}>
              <time>{readText(row, "effective_date") ?? "—"}</time>
              <div>
                <span>
                  {readText(row, "document_type") ?? "政策材料"}
                  {readText(analysis, "stance")
                    ? ` · ${stanceLabel(readText(analysis, "stance")!)}`
                    : ""}
                </span>
                <strong>{readText(row, "title") ?? "未命名政策材料"}</strong>
                <small>
                  {readText(row, "speaker_name") ?? "美联储机构材料"} ·{" "}
                  {readText(row, "role_title") ?? "机构材料"} ·{" "}
                  {voterLabel(readBoolean(row, "fomc_voter"))}
                </small>
              </div>
              {readText(row, "source_url") ? (
                <a href={readText(row, "source_url")!} rel="noreferrer" target="_blank">
                  原文
                </a>
              ) : null}
            </li>
          );
        })}
      </ol>
    </details>
  );
}

function PositionSourceList({ rows }: { rows: ReturnType<typeof parsePositions> }) {
  if (!rows.length) return null;
  return (
    <div className="macro-decision__position-sources">
      {rows.map((row) => (
        <article key={row.contractCode}>
          <strong>{row.contractName}</strong>
          <span>{row.reportDate ?? "—"}</span>
          {row.sourceUrl ? (
            <a href={row.sourceUrl} rel="noreferrer" target="_blank">
              CFTC 来源
            </a>
          ) : null}
        </article>
      ))}
    </div>
  );
}

function SettlementTable({ rows }: { rows: JsonObject[] }) {
  if (!rows.length) return null;
  return (
    <details className="macro-decision__settlements" open>
      <summary>VIX 官方结算（最近 {rows.length} 条）</summary>
      <div role="table">
        <div role="row">
          <span role="columnheader">交易日</span>
          <span role="columnheader">合约</span>
          <span role="columnheader">官方到期日</span>
          <span role="columnheader">结算</span>
          <span role="columnheader">持仓 / 成交</span>
          <span role="columnheader">来源时钟</span>
        </div>
        {rows.map((row, index) => (
          <div
            key={`${readText(row, "trade_date")}:${readText(row, "contract_code")}:${readText(row, "contract_expiration_date")}:${index}`}
            role="row"
          >
            <span role="cell">{readText(row, "trade_date") ?? "—"}</span>
            <span role="cell">{readText(row, "contract_code") ?? "—"}</span>
            <span role="cell">{readText(row, "contract_expiration_date") ?? "—"}</span>
            <span role="cell">{formatOptional(readNumber(row, "settlement_price"), "")}</span>
            <span role="cell">
              {formatOptional(readNumber(row, "open_interest"), "")} /{" "}
              {formatOptional(readNumber(row, "volume"), "")}
            </span>
            <span role="cell">
              发布{" "}
              {readNumber(row, "published_at_ms")
                ? formatInstant(readNumber(row, "published_at_ms") ?? 0)
                : "—"}
              <small>接收 {formatInstant(readNumber(row, "received_at_ms") ?? 0)}</small>
            </span>
          </div>
        ))}
      </div>
    </details>
  );
}

function indicatorSeries(indicators: MacroIndicatorView[]): MacroChartSeries[] {
  return indicators.map((indicator, index) => ({
    id: indicator.datasetId,
    label: indicator.label,
    color: chartColor(index),
    points: indicator.history,
  }));
}

function moduleChartAnnotations(
  module: MacroTypedModuleReadData,
  series: MacroChartSeries[],
): MacroChartAnnotation[] {
  const visibleDatasetIds = new Set(series.map((item) => item.id));
  const changes =
    module.module_id === "rates_fed"
      ? []
      : module.summary.top_changes.flatMap((change) =>
          change.as_of && visibleDatasetIds.has(change.dataset_id)
            ? [
                {
                  id: `change:${change.concept_id}:${change.dataset_id}:${change.as_of}`,
                  date: change.as_of,
                  detail: change.importance_explanation,
                  label: change.label,
                  seriesId: change.dataset_id,
                  tone: "change" as const,
                  value: change.value,
                },
              ]
            : [],
        );
  return changes;
}

function indicatorChangeGroups(indicators: MacroIndicatorView[]): MacroBarGroup[] {
  return indicators.map((indicator) => ({
    id: indicator.datasetId,
    label: shortIndicatorLabel(indicator.label),
    values: [
      { id: "1w", label: "1W", value: indicator.change1w, color: chartColor(0) },
      { id: "1m", label: "1M", value: indicator.change1m, color: chartColor(1) },
    ],
  }));
}

function positionGroups(rows: MacroPositionView[]): MacroBarGroup[] {
  return rows.map((row) => ({
    id: row.contractCode,
    label: row.contractName,
    values: [
      {
        id: "dealer",
        label: "Dealer",
        value: row.dealerNetPctOi,
        color: chartColor(0),
      },
      {
        id: "asset-manager",
        label: "Asset Manager",
        value: row.assetManagerNetPctOi,
        color: chartColor(1),
      },
      {
        id: "leveraged",
        label: "Leveraged",
        value: row.leveragedNetPctOi,
        color: chartColor(3),
      },
    ],
  }));
}

function crossAssetNormalizedSeries(group: MacroCrossAssetNormalizedGroupView): MacroChartSeries[] {
  return group.series.map((series, index) => ({
    id: series.source.datasetId,
    label: series.label,
    color: chartColor(index),
    points: series.points,
  }));
}

function commonUnit(indicators: MacroIndicatorView[]): string {
  const units = [...new Set(indicators.map((item) => item.unit).filter(Boolean))];
  return units.length === 1 ? (units[0] ?? "") : "";
}

function spreadLabel(value: string): string {
  return (
    {
      "2s10s": "2Y–10Y",
      "10s30s": "10Y–30Y",
      "3m10s": "3M–10Y",
      "5s30s": "5Y–30Y",
    }[value] ?? "期限利差"
  );
}

function ratesWindow(
  row: MacroRatesFedReadData["decision"]["tenor_matrix"][number],
  window: "1d" | "1w" | "mtd",
) {
  return row.windows.find((item) => item.window === window);
}

function ratesWindowLabel(value: string): string {
  return (
    {
      "1d": "1D",
      "1w": "1W",
      mtd: "MTD",
      "3m": "3M",
      past_30d: "过去30日",
    }[value] ?? value
  );
}

function ratesCompletenessLabel(value: string): string {
  return { complete: "完整", unaligned: "未对齐", incomplete: "不完整" }[value] ?? value;
}

function ratesDirection(value: number | null): string {
  if (value == null || value === 0) return "flat";
  return value > 0 ? "up" : "down";
}

function stanceLabel(value: string): string {
  return (
    {
      dovish: "鸽派",
      hawkish: "鹰派",
      mixed: "混合",
      neutral: "中性",
      no_call: "证据不足，暂不判断",
    }[value] ?? "立场未解释"
  );
}

function voterLabel(value: boolean | null): string {
  return value == null ? "机构材料" : value ? "当期投票" : "当期非投票";
}

function shortIndicatorLabel(value: string): string {
  return value.length > 16 ? `${value.slice(0, 15)}…` : value;
}

function chartColor(index: number): string {
  return [
    "#67d4ff",
    "#f6c85f",
    "#9d8cff",
    "#ff7c8b",
    "#66c2a5",
    "#fc8d62",
    "#8da0cb",
    "#e78ac3",
    "#a6d854",
    "#ffd92f",
  ][index % 10]!;
}

function EmptyState({ text }: { text: string }) {
  return <p className="macro-decision__empty">{text}</p>;
}

function formatInstant(value: number): string {
  return new Intl.DateTimeFormat("zh-CN", {
    dateStyle: "short",
    hour12: false,
    timeStyle: "short",
  }).format(new Date(value));
}

function formatOptional(value: number | null, unit: string | null): string {
  if (value == null) return "—";
  return `${formatNumber(value)}${unitLabel(unit)}`;
}

function formatSigned(value: number | null): string {
  if (value == null) return "—";
  return `${value > 0 ? "+" : ""}${formatNumber(value)}`;
}

function formatSignedBp(value: number | null): string {
  return value == null ? "—" : `${value > 0 ? "+" : ""}${formatNumber(value)}bp`;
}

function formatNumber(value: number): string {
  return new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 }).format(value);
}

function unitLabel(unit: string | null): string {
  if (!unit) return "";
  return (
    {
      basis_points: " bp",
      billions_chained_2017_usd: " 十亿 2017 年不变价美元",
      billions_usd: " 十亿美元",
      bp: " bp",
      index: " 点",
      index_points: " 点",
      millions_usd: " 百万美元",
      percent: "%",
      percent_open_interest: "% OI",
      thousands_persons: " 千人",
      usd_per_barrel: " 美元/桶",
      usdt: " USDT",
    }[unit] ?? "（单位未解释）"
  );
}
