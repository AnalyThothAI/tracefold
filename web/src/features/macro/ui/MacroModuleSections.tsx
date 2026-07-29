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
  parseAssets,
  parseCorrelations,
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
  type MacroAssetView,
  type MacroHistoryPoint,
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
  type MacroChartSeries,
} from "./MacroCharts";

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
  return (
    <div className="macro-decision__module-workspace">
      <div className="macro-decision__section-navigation">
        <nav aria-label={`${module.label}内部视图`} className="macro-decision__section-nav">
          {sections.map((section) => (
            <a
              aria-current={active === section.id ? "page" : undefined}
              href={`#${section.id}`}
              key={section.id}
            >
              {section.label}
            </a>
          ))}
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

function RatesSection({ module, active }: { module: MacroRatesFedReadData; active: string }) {
  if (active === "policy") {
    const indicators = parseIndicators(module.policy_pricing);
    return (
      <ModuleWorkbench
        description="EFFR、目标区间、SOFR 与 Treasury 短长端使用各自事实序列，不生成缺少底层合约的 FedWatch 近似概率。"
        eyebrow="POLICY CORRIDOR"
        module={module}
        title="政策走廊与当前市场定价"
      >
        <MacroTimeSeriesChart
          description="相同百分比坐标；默认按数据自然频率截取。"
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
  const classification = readRecord(curve, "classification");
  return (
    <ModuleWorkbench
      description="横轴始终是统一期限；名义、实际与 breakeven 分图，避免双轴与历史 sparkline 冒充收益率曲线。"
      eyebrow="TREASURY CURVE"
      module={module}
      title={`${readText(classification, "label") ?? readText(classification, "state") ?? "收益率曲线"} · 当前 / 1W / 1M / 3M`}
    >
      <div className="macro-decision__chart-stack">
        <MacroCurveChart snapshots={nominal} title="名义 Treasury 曲线" />
        <div className="macro-decision__chart-pair">
          <MacroCurveChart snapshots={real} title="实际 Treasury 曲线" />
          <MacroCurveChart snapshots={breakeven} title="Breakeven 通胀补偿" />
        </div>
        <MacroTimeSeriesChart
          baseline={0}
          description="2s10s、3m10s 与 5s30s，统一使用基点。"
          series={spreadSeries}
          title="关键期限利差"
          unit="basis_points"
        />
      </div>
    </ModuleWorkbench>
  );
}

function FedCommunication({ module }: { module: MacroRatesFedReadData }) {
  const fed = asRecord(module.fed);
  const institutional = readRecord(fed, "institutional_stance");
  const distribution = readRecord(fed, "officials_distribution");
  const eventCounts = firstNonEmptyRecord(
    readRecord(distribution, "stance_event_counts"),
    readRecord(fed, "event_counts"),
  );
  const officialCounts = firstNonEmptyRecord(
    readRecord(distribution, "stance_unique_official_counts"),
    readRecord(fed, "unique_official_counts"),
  );
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
      <div className="macro-decision__fed-callout">
        <span>机构立场</span>
        <strong>
          {readText(institutional, "direction") ?? readText(institutional, "state") ?? "no_call"}
        </strong>
        <p>{readText(institutional, "reason") ?? "尚未发布可审计的机构立场分析。"}</p>
      </div>
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
      description: "CPI、核心 CPI、PCE 与核心 PCE 分为同口径小图，并保留官方 surprise / revision。",
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
      <IndicatorSmallMultiples indicators={indicators} />
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
        <MacroTimeSeriesChart
          series={indicatorSeries(indicators)}
          title="SOFR 与 IORB"
          unit="percent"
        />
        <MacroTimeSeriesChart
          baseline={0}
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
      <IndicatorSmallMultiples indicators={indicators} />
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
        <MacroTimeSeriesChart
          series={indicatorSeries(indicators)}
          title="评级利差历史"
          unit={commonUnit(indicators)}
        />
        <p className="macro-decision__callout">
          CCC−BB 尾差：
          {formatOptional(readNumber(ladder, "tail_gap"), readText(ladder, "tail_gap_unit"))}
        </p>
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
        <MacroTimeSeriesChart
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
        <IndicatorSmallMultiples indicators={indicators} />
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
        <IndicatorSmallMultiples indicators={indicators} />
        <MacroIndicatorTable indicators={indicators} />
      </ModuleWorkbench>
    );
  }
  const confirmations = asRecord(module.confirmations);
  const assets = parseAssets(readRecords(confirmations, "etfs"));
  const positions = parsePositions(readRecords(confirmations, "positions"));
  return (
    <ModuleWorkbench
      description="LQD/HYG 与 CFTC 只作确认；每个价格事实保留 decision primary、intraday proxy 与 history 身份。"
      eyebrow="MARKET CONFIRMATION"
      module={module}
      title="信用 ETF 与仓位确认"
    >
      <AssetReturnMatrix assets={assets} />
      <SourceIdentityTable assets={assets} />
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
    const normalized = normalizedSeries(payload.normalized);
    return (
      <ModuleWorkbench
        description="VXN、GVZ 与 OVX 先按各自水平呈现；服务端归一化序列用于跨资产方向比较。"
        eyebrow="CROSS-ASSET IMPLIED VOL"
        module={module}
        title="股票、黄金与原油隐含波动率"
      >
        {normalized.length ? (
          <MacroTimeSeriesChart
            baseline={100}
            series={normalized}
            title="跨资产隐含波动率归一化走势"
            unit="index"
          />
        ) : (
          <IndicatorSmallMultiples indicators={indicators} />
        )}
        <MacroIndicatorTable indicators={indicators} />
      </ModuleWorkbench>
    );
  }
  const term = asRecord(module.term_structure);
  const indicators = parseIndicators(readRecords(term, "spot_and_three_month"));
  const spread = parseHistory(term.spread_history);
  return (
    <ModuleWorkbench
      description="这是 VIX 现货与三个月 VIX 的关系，不把两个点夸大为完整期货期限曲线。"
      eyebrow="SPOT–3M RELATIONSHIP"
      module={module}
      title="VIX 现货–3M 关系"
    >
      <MacroTimeSeriesChart
        series={indicatorSeries(indicators)}
        title="VIX 与 3M VIX"
        unit="index"
      />
      <MacroTimeSeriesChart
        baseline={0}
        series={[{ id: "vix-vxv", label: "VIX − 3M VIX", points: spread }]}
        title="现货减三个月利差"
        unit="index_points"
      />
      <MacroIndicatorTable indicators={indicators} />
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
    const series = normalizedSeries(assetsPayload.normalized);
    const groupBySymbol = new Map(
      [
        ...parseAssets(readRecords(assetsPayload, "proxies")),
        ...parseAssets(readRecords(assetsPayload, "benchmarks")),
      ].map((asset) => [asset.symbol, asset.assetClass]),
    );
    const groups: ReadonlyArray<{
      id: string;
      label: string;
      assetClasses: ReadonlySet<string>;
    }> = [
      { id: "equity", label: "权益", assetClasses: new Set(["equity"]) },
      {
        id: "duration-credit",
        label: "久期与信用",
        assetClasses: new Set(["rates", "credit"]),
      },
      {
        id: "dollar-commodities",
        label: "美元与商品",
        assetClasses: new Set(["fx", "commodity", "other"]),
      },
    ];
    return (
      <ModuleWorkbench
        description="各资产由服务端归一为窗口首日 100，并按权益、久期与信用、美元与商品三组拆图；不同价格单位不会直接比较。"
        eyebrow="NORMALIZED GROUPS"
        module={module}
        title="大类资产分组归一化走势"
      >
        <div className="macro-decision__chart-stack">
          {groups.map((group) => {
            const matching = series.filter((item) =>
              group.assetClasses.has(groupBySymbol.get(item.id) ?? "other"),
            );
            return matching.length ? (
              <MacroTimeSeriesChart
                baseline={100}
                key={group.id}
                series={matching}
                title={group.label}
                unit="index"
              />
            ) : null;
          })}
        </div>
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
    const market = parseAssets(readRecords(futures, "market"));
    const positions = parsePositions(readRecords(futures, "positions"));
    return (
      <ModuleWorkbench
        description="期货市场、VIX 官方结算与 CFTC 周度仓位分层呈现，不混用来源时钟。"
        eyebrow="FUTURES CONFIRMATION"
        module={module}
        title="期货市场与仓位确认"
      >
        <AssetReturnMatrix assets={market} />
        <SourceIdentityTable assets={market} />
        <MacroBarChart
          groups={positionGroups(positions)}
          title="跨资产 CFTC 净仓位"
          unit="percent_open_interest"
        />
        <SettlementTable rows={readRecords(futures, "vix_settlements")} />
      </ModuleWorkbench>
    );
  }
  const benchmarks = parseAssets(readRecords(assetsPayload, "benchmarks"));
  const proxies = parseAssets(readRecords(assetsPayload, "proxies"));
  const combined = benchmarks.length ? benchmarks : proxies;
  return (
    <ModuleWorkbench
      description="固定十二资产用 1D / 1W / 1M 收益热力矩阵开场；基准和可交易代理的来源身份始终分离。"
      eyebrow="CROSS-ASSET RETURNS"
      module={module}
      title="固定资产篮子收益矩阵"
    >
      <AssetReturnMatrix assets={combined} />
      <SourceIdentityTable assets={combined} />
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
  const latestChange = module.summary.top_changes[0];
  return (
    <aside aria-label="图表决策注释" className="macro-decision__annotation-rail">
      <header>
        <span>DECISION ANNOTATIONS</span>
        <strong>只读 API 结论</strong>
      </header>
      <article>
        <span>事实截点</span>
        <strong>{formatInstant(module.latest_fact_at_ms)}</strong>
        <small>{module.summary.headline}</small>
      </article>
      <article data-state="change">
        <span>关键变化</span>
        <strong>{latestChange?.label ?? "尚无足够历史"}</strong>
        <small>{latestChange?.importance_explanation ?? "等待自然频率积累。"}</small>
      </article>
      <article data-state="weakening">
        <span>矛盾</span>
        <strong>{module.contradictions[0] ?? "暂未识别结构性矛盾"}</strong>
      </article>
      <article data-state="invalidated">
        <span>失效条件</span>
        <strong>{module.falsifiers[0] ?? "暂无预设失效条件"}</strong>
      </article>
      <article>
        <span>下一检查点</span>
        <strong>{module.next_checkpoints[0]?.label ?? "暂无待补检查点"}</strong>
        <small>{module.next_checkpoints[0]?.next_check ?? "按自然发布频率检查"}</small>
      </article>
    </aside>
  );
}

function IndicatorSmallMultiples({ indicators }: { indicators: MacroIndicatorView[] }) {
  if (!indicators.length) return <EmptyState text="当前没有可用指标。" />;
  return (
    <div className="macro-decision__small-multiples">
      {indicators.map((indicator) => (
        <MacroTimeSeriesChart
          key={indicator.datasetId}
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
  if (!rows.length) return <EmptyState text="信用周期维度尚未形成。" />;
  return (
    <div aria-label="信用周期四维结论" className="macro-decision__credit-dimensions">
      {rows.map((row, index) => {
        const conflicts = asRecords(row.conflicts).length
          ? asRecords(row.conflicts)
              .map((item) => readText(item, "text"))
              .filter(Boolean)
          : Array.isArray(row.conflicts)
            ? row.conflicts.filter((item): item is string => typeof item === "string")
            : [];
        return (
          <article
            data-state={readText(row, "state") ?? "unknown"}
            key={readText(row, "dimension_id") ?? index}
          >
            <span>{readText(row, "label") ?? `维度 ${index + 1}`}</span>
            <strong>{readText(row, "state") ?? "insufficient"}</strong>
            <p>{readText(row, "driver") ?? "等待事实回填。"}</p>
            {conflicts.map((conflict) => (
              <small key={conflict}>{conflict}</small>
            ))}
          </article>
        );
      })}
    </div>
  );
}

function AssetReturnMatrix({ assets }: { assets: MacroAssetView[] }) {
  if (!assets.length) return <EmptyState text="固定资产篮子尚未回填。" />;
  return (
    <div className="macro-decision__return-matrix" role="table">
      <div role="row">
        <span role="columnheader">资产</span>
        <span role="columnheader">最新</span>
        <span role="columnheader">1D</span>
        <span role="columnheader">1W</span>
        <span role="columnheader">1M</span>
      </div>
      {assets.map((asset) => {
        const fact = asset.history ?? asset.decisionPrimary ?? asset.intradayProxy;
        const current = asset.decisionPrimary ?? asset.intradayProxy ?? asset.history;
        return (
          <div key={`${asset.symbol}:${asset.label}`} role="row">
            <span role="cell">
              <strong>{asset.symbol}</strong>
              <small>{asset.label}</small>
            </span>
            <span role="cell">{formatOptional(current?.latestValue ?? null, "")}</span>
            <HeatCell value={fact?.change1dPct ?? null} />
            <HeatCell value={fact?.change1wPct ?? null} />
            <HeatCell value={fact?.change1mPct ?? null} />
          </div>
        );
      })}
    </div>
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

function SourceIdentityTable({ assets }: { assets: MacroAssetView[] }) {
  if (!assets.length) return null;
  return (
    <details className="macro-decision__source-table">
      <summary>查看来源身份与时钟（{assets.length}）</summary>
      <div role="table">
        <div role="row">
          <span role="columnheader">资产</span>
          <span role="columnheader">Decision Primary</span>
          <span role="columnheader">Intraday Proxy</span>
          <span role="columnheader">History</span>
          <span role="columnheader">身份规则</span>
        </div>
        {assets.map((asset) => (
          <div key={`${asset.symbol}:${asset.label}`} role="row">
            <span role="cell">
              <strong>{asset.symbol}</strong>
              <small>{asset.evidenceKind || asset.assetClass}</small>
            </span>
            <SourceFact fact={asset.decisionPrimary} />
            <SourceFact fact={asset.intradayProxy} />
            <SourceFact fact={asset.history} />
            <span role="cell">
              {asset.identityPolicy || asset.selectionPolicy || "separate_source_facts_no_blend"}
            </span>
          </div>
        ))}
      </div>
    </details>
  );
}

function SourceFact({ fact }: { fact: MacroAssetView["decisionPrimary"] }) {
  if (!fact) return <span role="cell">—</span>;
  return (
    <span role="cell">
      <strong>{fact.datasetId}</strong>
      <small>{fact.asOf ?? (fact.marketTimeMs ? formatInstant(fact.marketTimeMs) : "—")}</small>
      {fact.sourceUrl ? (
        <a href={fact.sourceUrl} rel="noreferrer" target="_blank">
          来源 <ExternalLink aria-hidden="true" />
        </a>
      ) : null}
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
                  {readText(row, "document_type") ?? "document"} ·{" "}
                  {readText(analysis, "stance") ?? "no_call"}
                </span>
                <strong>{readText(row, "title") ?? "未命名政策材料"}</strong>
                <small>
                  {readText(row, "speaker_name") ?? "FOMC institution"} ·{" "}
                  {readText(row, "role_title") ?? "institutional"} ·{" "}
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
    <details className="macro-decision__settlements">
      <summary>VIX 官方结算（最近 {rows.length} 条）</summary>
      <div role="table">
        <div role="row">
          <span role="columnheader">交易日</span>
          <span role="columnheader">合约</span>
          <span role="columnheader">结算</span>
          <span role="columnheader">持仓 / 成交</span>
        </div>
        {rows.map((row, index) => (
          <div
            key={`${readText(row, "trade_date")}:${readText(row, "contract_code")}:${index}`}
            role="row"
          >
            <span role="cell">{readText(row, "trade_date") ?? "—"}</span>
            <span role="cell">{readText(row, "contract_code") ?? "—"}</span>
            <span role="cell">{formatOptional(readNumber(row, "settlement_price"), "")}</span>
            <span role="cell">
              {formatOptional(readNumber(row, "open_interest"), "")} /{" "}
              {formatOptional(readNumber(row, "volume"), "")}
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

function normalizedSeries(value: unknown): MacroChartSeries[] {
  const grouped = new Map<string, MacroHistoryPoint[]>();
  asRecords(value).forEach((row) => {
    const symbol = readText(row, "symbol");
    const date = readText(row, "date");
    const normalizedValue = readNumber(row, "normalized_value");
    if (!symbol || !date || normalizedValue == null) return;
    const points = grouped.get(symbol) ?? [];
    points.push({ date, value: normalizedValue });
    grouped.set(symbol, points);
  });
  return [...grouped.entries()].map(([symbol, points], index) => ({
    id: symbol,
    label: symbol,
    color: chartColor(index),
    points: points.sort((left, right) => left.date.localeCompare(right.date)),
  }));
}

function firstNonEmptyRecord(...records: JsonObject[]): JsonObject {
  return records.find((record) => Object.keys(record).length) ?? {};
}

function commonUnit(indicators: MacroIndicatorView[]): string {
  const units = [...new Set(indicators.map((item) => item.unit).filter(Boolean))];
  return units.length === 1 ? (units[0] ?? "") : "";
}

function spreadLabel(value: string): string {
  return (
    {
      "2s10s": "2Y–10Y",
      "3m10s": "3M–10Y",
      "5s30s": "5Y–30Y",
    }[value] ?? value
  );
}

function stanceLabel(value: string): string {
  return (
    {
      dovish: "鸽派",
      hawkish: "鹰派",
      mixed: "混合",
      neutral: "中性",
    }[value] ?? value
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

function formatNumber(value: number): string {
  return new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 }).format(value);
}

function unitLabel(unit: string | null): string {
  if (!unit) return "";
  return (
    {
      basis_points: " bp",
      billions_usd: " 十亿美元",
      index: " 点",
      index_points: " 点",
      millions_usd: " 百万美元",
      percent: "%",
      percent_open_interest: "% OI",
      thousands_persons: " 千人",
      usd_per_barrel: " 美元/桶",
    }[unit] ?? ` ${unit}`
  );
}
