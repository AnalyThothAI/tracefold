import { ExternalLink } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import type { CSSProperties } from "react";

import type {
  JsonObject,
  MacroAssetRow,
  MacroCreditReadData,
  MacroCurveSnapshot,
  MacroEconomyInflationReadData,
  MacroIndicator,
  MacroLiquidityFundingReadData,
  MacroRatesFedReadData,
  MacroTypedModuleReadData,
  MacroVolatilityReadData,
} from "../model/macroTypes";

import "./MacroModuleSections.css";

type Section = { id: string; label: string };

const SECTIONS: Record<MacroTypedModuleReadData["module_id"], Section[]> = {
  rates_fed: [
    { id: "curve", label: "收益率曲线" },
    { id: "policy", label: "政策定价" },
    { id: "fed", label: "FOMC 与讲话" },
    { id: "positioning", label: "期货仓位" },
  ],
  economy_inflation: [
    { id: "inflation", label: "通胀" },
    { id: "labor", label: "就业" },
    { id: "growth", label: "增长" },
  ],
  liquidity_funding: [
    { id: "balance-sheet", label: "资产负债表" },
    { id: "funding", label: "资金市场" },
  ],
  credit: [
    { id: "spreads", label: "评级梯级" },
    { id: "funding", label: "融资成本" },
    { id: "banks", label: "银行供给" },
    { id: "quality", label: "贷款质量" },
    { id: "confirmation", label: "市场确认" },
  ],
  volatility: [
    { id: "term", label: "期限结构" },
    { id: "cross-asset", label: "跨资产波动率" },
  ],
  cross_asset: [
    { id: "assets", label: "资产矩阵" },
    { id: "normalized", label: "归一化比较" },
    { id: "correlations", label: "相关性" },
    { id: "futures", label: "期货确认" },
  ],
};

export function MacroModuleSections({ module }: { module: MacroTypedModuleReadData }) {
  const sections = SECTIONS[module.module_id];
  const active = useHashSection(sections);
  return (
    <>
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
      {module.module_id === "rates_fed" ? <RatesSections active={active} module={module} /> : null}
      {module.module_id === "economy_inflation" ? (
        <EconomySections active={active} module={module} />
      ) : null}
      {module.module_id === "liquidity_funding" ? (
        <LiquiditySections active={active} module={module} />
      ) : null}
      {module.module_id === "credit" ? <CreditSections active={active} module={module} /> : null}
      {module.module_id === "volatility" ? (
        <VolatilitySections active={active} module={module} />
      ) : null}
      {module.module_id === "cross_asset" ? (
        <CrossAssetSections active={active} module={module} />
      ) : null}
    </>
  );
}

function useHashSection(sections: Section[]) {
  const fallback = sections[0].id;
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

function RatesSections({ module, active }: { module: MacroRatesFedReadData; active: string }) {
  if (active === "policy") {
    return (
      <section className="macro-decision__semantic-section">
        <SectionHeading
          eyebrow="POLICY PRICING"
          title="现金政策、政策走廊与曲线定价"
          description="EFFR、目标区间、SOFR 与 Treasury 短长端并列；没有底层合约时不生成 FedWatch 概率。"
        />
        <IndicatorTable indicators={module.policy_pricing.rates} />
        <UnavailableCard
          label="CME 政策利率期货概率"
          reason={module.policy_pricing.cme_policy_probabilities.reason}
        />
      </section>
    );
  }
  if (active === "fed") {
    return (
      <section className="macro-decision__semantic-section">
        <SectionHeading
          eyebrow="FED COMMUNICATION"
          title="政策事件优先，不给官员贴永久标签"
          description="机构立场与官员讲话分布是两条独立轨道；每个结论绑定原文与不可变分析。"
        />
        <div className="macro-decision__fed-summary">
          <article>
            <span>FOMC 机构立场</span>
            <strong>{module.fed.institutional_stance.direction}</strong>
            <p>{module.fed.institutional_stance.reason}</p>
          </article>
          <article>
            <span>官员沟通分布</span>
            <strong>
              鹰 {module.fed.officials_distribution.hawkish} · 中性{" "}
              {module.fed.officials_distribution.neutral} · 鸽{" "}
              {module.fed.officials_distribution.dovish} · 混合{" "}
              {module.fed.officials_distribution.mixed}
            </strong>
            <p>
              {module.fed.officials_distribution.window_days} 日窗口，截至{" "}
              {module.fed.officials_distribution.as_of ?? "—"}；非政策{" "}
              {module.fed.officials_distribution.not_policy_signal} · 不确定{" "}
              {module.fed.officials_distribution.uncertain}
            </p>
          </article>
        </div>
        <FedTimeline events={module.fed.timeline} />
        {module.fed.roster.state !== "current" ? (
          <UnavailableCard label="有效日期官员 roster" reason={module.fed.roster.reason} />
        ) : null}
      </section>
    );
  }
  if (active === "positioning") {
    return (
      <section className="macro-decision__semantic-section">
        <SectionHeading
          eyebrow="POSITIONING"
          title="利率期货杠杆资金仓位"
          description="CFTC 仓位是市场确认，不替代收益率曲线与政策事实。"
        />
        <ObjectTable rows={module.positioning} />
      </section>
    );
  }
  return (
    <section className="macro-decision__semantic-section">
      <SectionHeading
        eyebrow="TREASURY CURVE"
        title={`${module.curve.classification.label} · 当前、1周、1月、3月`}
        description="横轴是统一期限，纵轴是统一收益率；不再把不同刻度的历史 sparkline 误称为收益率曲线。"
      />
      <CurveFigure snapshots={module.curve.nominal_snapshots} title="名义 Treasury 曲线" />
      <CurveFigure snapshots={module.curve.real_snapshots} title="实际 Treasury 曲线" />
      <CurveTenorTable curve={module.curve} />
      <SpreadTable spreads={module.curve.spreads} />
      <p className="macro-decision__callout">
        1周因子：水平 {numberLabel(module.curve.classification.inputs.level_change_bp)} bp · 斜率{" "}
        {numberLabel(module.curve.classification.inputs.slope_change_bp)} bp · 曲率{" "}
        {numberLabel(module.curve.classification.inputs.curvature_change_bp)} bp ·{" "}
        {module.curve.classification.formula_version}
      </p>
    </section>
  );
}

function EconomySections({
  module,
  active,
}: {
  module: MacroEconomyInflationReadData;
  active: string;
}) {
  const section =
    active === "labor" ? module.labor : active === "growth" ? module.growth : module.inflation;
  const releases =
    active === "growth"
      ? []
      : active === "labor"
        ? module.labor.official_releases
        : module.inflation.official_releases;
  return (
    <section className="macro-decision__semantic-section">
      <SectionHeading
        eyebrow="ECONOMY & INFLATION"
        title={active === "labor" ? "就业与劳动市场" : active === "growth" ? "增长与需求" : "通胀"}
        description="水平、1周/1月变化、样本范围与正式发布事实分开呈现。"
      />
      <IndicatorTable indicators={section.indicators} />
      {releases.length ? <ObjectTable rows={releases} /> : null}
    </section>
  );
}

function LiquiditySections({
  module,
  active,
}: {
  module: MacroLiquidityFundingReadData;
  active: string;
}) {
  const indicators =
    active === "funding" ? module.funding.indicators : module.balance_sheet.indicators;
  return (
    <section className="macro-decision__semantic-section">
      <SectionHeading
        eyebrow="LIQUIDITY & FUNDING"
        title={active === "funding" ? "SOFR 与准备金利率" : "央行、准备金与财政流动性"}
        description="余额与资金价格分开读，避免把同方向变化机械压成一个总分。"
      />
      <IndicatorTable indicators={indicators} />
    </section>
  );
}

function CreditSections({ module, active }: { module: MacroCreditReadData; active: string }) {
  let content;
  if (active === "funding") {
    content = (
      <section className="macro-decision__semantic-section">
        <SectionHeading
          eyebrow="ALL-IN FUNDING COST"
          title="公司债绝对融资成本"
          description="IG/HY effective yield 与 Fed Funds、10Y Treasury 并列，紧利差不再等同于低融资成本。"
        />
        <IndicatorTable
          indicators={[
            ...module.funding_costs.corporate_yields,
            ...module.funding_costs.reference_rates,
          ]}
        />
        <FundingComparisonTable rows={module.funding_costs.comparisons} />
      </section>
    );
  } else if (active === "banks") {
    content = (
      <section className="macro-decision__semantic-section">
        <SectionHeading
          eyebrow="BANK SUPPLY"
          title="SLOOS 贷款标准与需求"
          description="C&I、CRE 与消费信贷供给/需求使用官方季度时钟。"
        />
        <IndicatorTable indicators={module.bank_lending.indicators} />
      </section>
    );
  } else if (active === "quality") {
    content = (
      <section className="macro-decision__semantic-section">
        <SectionHeading
          eyebrow="REALIZED QUALITY"
          title="逾期率与核销率"
          description="市场定价必须接受企业、CRE 与消费贷款真实质量的交叉检验。"
        />
        <IndicatorTable indicators={module.loan_quality.indicators} />
      </section>
    );
  } else if (active === "confirmation") {
    content = (
      <section className="macro-decision__semantic-section">
        <SectionHeading
          eyebrow="MARKET CONFIRMATION"
          title="ETF 与期货仓位确认"
          description="LQD/HYG 和 CFTC 只作确认；TRACE 与 NAV 细节在获得合规数据前保持不可用。"
        />
        <AssetTable rows={module.confirmations.etfs} />
        <ObjectTable rows={module.confirmations.positions} />
        <UnavailableCard label="TRACE / ETF NAV" reason={module.confirmations.trace_nav.reason} />
      </section>
    );
  } else {
    content = (
      <section className="macro-decision__semantic-section">
        <SectionHeading
          eyebrow="CREDIT LADDER"
          title="IG → BBB → BB → B → CCC 评级梯级"
          description="同时展示水平、变化、实际样本数与历史分位；没有综合信用分数。"
        />
        <IndicatorTable indicators={module.spread_ladder.rows} />
        <p className="macro-decision__callout">
          CCC–BB 尾差：{formatSigned(module.spread_ladder.tail_gap)} bp
        </p>
      </section>
    );
  }
  return (
    <>
      <CreditCycleDimensions rows={module.cycle_dimensions} />
      {content}
    </>
  );
}

function CreditCycleDimensions({ rows }: { rows: MacroCreditReadData["cycle_dimensions"] }) {
  return (
    <section aria-label="信用周期五维结论" className="macro-decision__credit-dimensions">
      {rows.map((row) => (
        <article data-state={row.state} key={row.dimension_id}>
          <span>{row.label}</span>
          <strong>{row.state}</strong>
          <p>{row.driver}</p>
          {row.conflicts.map((conflict) => (
            <small key={conflict}>{conflict}</small>
          ))}
        </article>
      ))}
    </section>
  );
}

function VolatilitySections({
  module,
  active,
}: {
  module: MacroVolatilityReadData;
  active: string;
}) {
  const indicators =
    active === "cross-asset"
      ? module.cross_asset_implied.indicators
      : module.term_structure.spot_and_three_month;
  return (
    <section className="macro-decision__semantic-section">
      <SectionHeading
        eyebrow="VOLATILITY"
        title={active === "cross-asset" ? "股票、黄金与原油隐含波动率" : "VIX 现货与三个月期限结构"}
        description="期限与跨资产波动率分开，保留各自来源和历史范围。"
      />
      <IndicatorTable indicators={indicators} />
    </section>
  );
}

function CrossAssetSections({
  module,
  active,
}: {
  module: Extract<MacroTypedModuleReadData, { module_id: "cross_asset" }>;
  active: string;
}) {
  if (active === "normalized") {
    return (
      <section className="macro-decision__semantic-section">
        <SectionHeading
          eyebrow="NORMALIZED TO 100"
          title="固定 ETF 篮子归一化比较"
          description="各资产从窗口首日归一为 100，避免不同价格单位造成错误比较。"
        />
        <NormalizedFigure points={module.assets.normalized} />
      </section>
    );
  }
  if (active === "correlations") {
    return (
      <section className="macro-decision__semantic-section">
        <SectionHeading
          eyebrow="CORRELATION"
          title="最多 120 个日收益样本"
          description="相关性是独立证据，不挤占默认资产矩阵。"
        />
        <CorrelationTable rows={module.correlations} />
      </section>
    );
  }
  if (active === "futures") {
    return (
      <section className="macro-decision__semantic-section">
        <SectionHeading
          eyebrow="FUTURES CONFIRMATION"
          title="VIX 结算与跨资产 CFTC 仓位"
          description="期货数据只作确认；本 Issue 不建设 WTI 曲线或原油 CFTC 模型。"
        />
        <ObjectTable rows={[...module.futures.vix_settlements, ...module.futures.positions]} />
      </section>
    );
  }
  return (
    <section className="macro-decision__semantic-section">
      <SectionHeading
        eyebrow="CROSS-ASSET MATRIX"
        title="市场基准与可交易 ETF 代理分开"
        description="USO 只是 ETF 行；WTI Cushing spot 是独立官方基准。"
      />
      <BenchmarkTable rows={module.assets.benchmarks} />
      <AssetTable rows={module.assets.proxies} />
      <NormalizedFigure points={module.assets.normalized} />
    </section>
  );
}

function SectionHeading({
  eyebrow,
  title,
  description,
}: {
  eyebrow: string;
  title: string;
  description: string;
}) {
  return (
    <header className="macro-decision__section-heading">
      <span>{eyebrow}</span>
      <h2>{title}</h2>
      <p>{description}</p>
    </header>
  );
}

function CurveFigure({ snapshots, title }: { snapshots: MacroCurveSnapshot[]; title: string }) {
  const allValues = snapshots.flatMap((snapshot) =>
    snapshot.points.map((point) => point.yield_pct),
  );
  if (!allValues.length) return <EmptyState text={`${title}尚在回填。`} />;
  const minimum = Math.min(...allValues);
  const maximum = Math.max(...allValues);
  const span = maximum - minimum || 1;
  return (
    <figure className="macro-decision__curve">
      <figcaption>{title}</figcaption>
      <svg aria-label={title} preserveAspectRatio="none" viewBox="0 0 100 42">
        {snapshots.map((snapshot) => {
          const path = snapshot.points
            .map((point, index) => {
              const x =
                snapshot.points.length === 1 ? 50 : (index / (snapshot.points.length - 1)) * 100;
              const y = 38 - ((point.yield_pct - minimum) / span) * 32;
              return `${index ? "L" : "M"} ${x.toFixed(2)} ${y.toFixed(2)}`;
            })
            .join(" ");
          return <path data-window={snapshot.window} d={path} key={snapshot.window} />;
        })}
      </svg>
      <div className="macro-decision__curve-legend">
        {snapshots.map((snapshot) => (
          <span data-window={snapshot.window} key={snapshot.window}>
            {windowLabel(snapshot.window)} · {snapshot.as_of}
          </span>
        ))}
      </div>
      <div className="macro-decision__curve-tenors">
        {snapshots[0].points.map((point) => (
          <span key={point.tenor}>{point.tenor}</span>
        ))}
      </div>
    </figure>
  );
}

function CurveTenorTable({ curve }: { curve: MacroRatesFedReadData["curve"] }) {
  const nominalByWindow = new Map(
    curve.nominal_snapshots.map((snapshot) => [
      snapshot.window,
      new Map(snapshot.points.map((point) => [point.tenor, point.yield_pct])),
    ]),
  );
  const current = curve.nominal_snapshots.find((snapshot) => snapshot.window === "current");
  const real = new Map(
    (curve.real_snapshots.find((snapshot) => snapshot.window === "current")?.points ?? []).map(
      (point) => [point.tenor, point.yield_pct],
    ),
  );
  const breakeven = new Map(
    (curve.breakeven_snapshots.find((snapshot) => snapshot.window === "current")?.points ?? []).map(
      (point) => [point.tenor, point.breakeven_pct],
    ),
  );
  if (!current) return <EmptyState text="详细期限表等待 Treasury 曲线回填。" />;
  return (
    <div className="macro-decision__semantic-table macro-decision__curve-table" role="table">
      <div role="row">
        <span role="columnheader">期限</span>
        <span role="columnheader">当前名义</span>
        <span role="columnheader">1周前</span>
        <span role="columnheader">1月前</span>
        <span role="columnheader">3月前</span>
        <span role="columnheader">当前实际 / breakeven</span>
      </div>
      {current.points.map((point) => (
        <div key={point.tenor} role="row">
          <span role="cell">
            <strong>{point.tenor}</strong>
          </span>
          <span role="cell">{formatNumber(point.yield_pct)}%</span>
          <span role="cell">{optionalPercent(nominalByWindow.get("1w")?.get(point.tenor))}</span>
          <span role="cell">{optionalPercent(nominalByWindow.get("1m")?.get(point.tenor))}</span>
          <span role="cell">{optionalPercent(nominalByWindow.get("3m")?.get(point.tenor))}</span>
          <span role="cell">
            {optionalPercent(real.get(point.tenor))} / {optionalPercent(breakeven.get(point.tenor))}
          </span>
        </div>
      ))}
    </div>
  );
}

function SpreadTable({
  spreads,
}: {
  spreads: Record<string, Array<{ date: string; value_bp: number }>>;
}) {
  return (
    <div className="macro-decision__metric-grid">
      {Object.entries(spreads).map(([name, points]) => (
        <article key={name}>
          <span>{name}</span>
          <strong>{points.length ? `${formatNumber(points.at(-1)!.value_bp)} bp` : "—"}</strong>
          <small>{points.length ? `${points[0].date} → ${points.at(-1)!.date}` : "等待回填"}</small>
        </article>
      ))}
    </div>
  );
}

function FundingComparisonTable({
  rows,
}: {
  rows: MacroCreditReadData["funding_costs"]["comparisons"];
}) {
  if (!rows.length) return <EmptyState text="等待公司债收益率与参考利率的共同日期。" />;
  return (
    <div className="macro-decision__metric-grid">
      {rows.map((row) => (
        <article key={`${row.corporate_dataset_id}-${row.reference_dataset_id}`}>
          <span>{row.label}</span>
          <strong>{formatSigned(row.value_bp)} bp</strong>
          <small>
            {row.as_of} · {row.formula_version}
          </small>
        </article>
      ))}
    </div>
  );
}

function IndicatorTable({ indicators }: { indicators: MacroIndicator[] }) {
  if (!indicators.length) return <EmptyState text="等待有效历史数据。" />;
  return (
    <div className="macro-decision__semantic-table" role="table">
      <div role="row">
        <span role="columnheader">指标</span>
        <span role="columnheader">最新</span>
        <span role="columnheader">1周</span>
        <span role="columnheader">1月</span>
        <span role="columnheader">样本 / 分位</span>
        <span role="columnheader">截至</span>
      </div>
      {indicators.map((item) => (
        <div key={item.dataset_id} role="row">
          <span role="cell">
            <strong>{item.label}</strong>
            <small>{item.dataset_id}</small>
          </span>
          <span role="cell">
            {formatNumber(item.latest_value)} {unitLabel(item.unit)}
          </span>
          <span role="cell">{formatSigned(item.change_1w)}</span>
          <span role="cell">{formatSigned(item.change_1m)}</span>
          <span role="cell">
            {item.sample_count} /{" "}
            {item.percentile == null ? "—" : `${formatNumber(item.percentile)}%`}
          </span>
          <span role="cell">
            <time>{item.as_of}</time>
            <SourceLink href={item.source_url} />
          </span>
        </div>
      ))}
    </div>
  );
}

function AssetTable({ rows }: { rows: MacroAssetRow[] }) {
  if (!rows.length) return <EmptyState text="固定 ETF 篮子尚在回填。" />;
  return (
    <div className="macro-decision__semantic-table macro-decision__asset-table" role="table">
      <div role="row">
        <span role="columnheader">ETF / 类型</span>
        <span role="columnheader">最新</span>
        <span role="columnheader">1日</span>
        <span role="columnheader">1周</span>
        <span role="columnheader">1月</span>
        <span role="columnheader">来源层级</span>
      </div>
      {rows.map((row) => (
        <div key={row.dataset_id} role="row">
          <span role="cell">
            <strong>{row.symbol}</strong>
            <small>
              {row.label} · {row.instrument_type}
            </small>
          </span>
          <span role="cell">
            {formatNumber(row.latest_value)} <small>{row.as_of}</small>
          </span>
          <SignedCell value={row.change_1d_pct} />
          <SignedCell value={row.change_1w_pct} />
          <SignedCell value={row.change_1m_pct} />
          <span role="cell">
            <small>{row.trust_tier}</small>
            <SourceLink href={row.source_url} />
          </span>
        </div>
      ))}
    </div>
  );
}

function BenchmarkTable({ rows }: { rows: JsonObject[] }) {
  return (
    <div className="macro-decision__benchmark-grid">
      {rows.map((row) => (
        <article key={textValue(row.dataset_id) ?? textValue(row.label) ?? "benchmark"}>
          <span>{textValue(row.asset_class) ?? "market"}</span>
          <strong>{textValue(row.label) ?? "基准"}</strong>
          <p>
            {numberLabel(row.latest_value)} · 1周 {numberLabel(row.change_1w)}
          </p>
          <small>{textValue(row.evidence_kind) ?? "reference"}</small>
        </article>
      ))}
    </div>
  );
}

function NormalizedFigure({
  points,
}: {
  points: Array<{ symbol: string; date: string; normalized_value: number }>;
}) {
  const symbols = [...new Set(points.map((point) => point.symbol))];
  const values = points.map((point) => point.normalized_value);
  if (!values.length) return <EmptyState text="等待 ETF 历史回填。" />;
  const minimum = Math.min(...values);
  const maximum = Math.max(...values);
  const span = maximum - minimum || 1;
  return (
    <figure className="macro-decision__normalized">
      <svg aria-label="固定 ETF 篮子归一化走势" preserveAspectRatio="none" viewBox="0 0 100 44">
        {symbols.map((symbol, symbolIndex) => {
          const series = points.filter((point) => point.symbol === symbol);
          const path = series
            .map((point, index) => {
              const x = series.length === 1 ? 50 : (index / (series.length - 1)) * 100;
              const y = 40 - ((point.normalized_value - minimum) / span) * 36;
              return `${index ? "L" : "M"} ${x.toFixed(2)} ${y.toFixed(2)}`;
            })
            .join(" ");
          return (
            <path
              d={path}
              key={symbol}
              style={{ "--series-index": symbolIndex } as CSSProperties}
            />
          );
        })}
      </svg>
      <figcaption>
        {symbols.map((symbol) => (
          <span key={symbol}>{symbol}</span>
        ))}
      </figcaption>
    </figure>
  );
}

function CorrelationTable({
  rows,
}: {
  rows: Array<{
    left: string;
    right: string;
    correlation: number | null;
    sample_count: number;
    window: string;
  }>;
}) {
  if (!rows.length) return <EmptyState text="至少需要 20 个共同日收益样本。" />;
  return (
    <div className="macro-decision__correlations">
      {rows.map((row) => (
        <article key={`${row.left}-${row.right}`}>
          <span>
            {row.left} / {row.right}
          </span>
          <strong>{row.correlation == null ? "—" : row.correlation.toFixed(2)}</strong>
          <small>n={row.sample_count}</small>
        </article>
      ))}
    </div>
  );
}

function FedTimeline({ events }: { events: MacroRatesFedReadData["fed"]["timeline"] }) {
  if (!events.length) return <EmptyState text="FOMC 文件与讲话正文尚在回填。" />;
  return (
    <ol className="macro-decision__timeline">
      {events.map((event) => (
        <li key={event.document_id}>
          <time>{event.effective_date}</time>
          <div>
            <span>
              {event.document_type} · {event.analysis.stance}
            </span>
            <strong>{event.title}</strong>
            <small>
              {event.speaker_name ?? "FOMC institution"} · {event.role_title ?? "institutional"} ·{" "}
              {event.fomc_voter == null ? "机构材料" : event.fomc_voter ? "当期投票" : "当期非投票"}
            </small>
            {event.analysis.state === "analyzed" ? (
              <details>
                <summary>
                  {event.analysis.policy_relevance} · confidence{" "}
                  {event.analysis.confidence == null ? "—" : event.analysis.confidence.toFixed(2)}
                </summary>
                <ul>
                  {event.analysis.evidence.map((item, index) => (
                    <li key={`${event.document_id}-evidence-${index}`}>
                      {textValue(item.claim) ?? "证据"}：{textValue(item.excerpt) ?? "—"}
                    </li>
                  ))}
                </ul>
                <small>
                  {event.analysis.model_name ?? "deterministic"} ·{" "}
                  {event.analysis.prompt_version ?? "—"} ·{" "}
                  {event.analysis.reviewer_disposition ?? "—"}
                </small>
              </details>
            ) : null}
          </div>
          <SourceLink href={event.source_url} />
        </li>
      ))}
    </ol>
  );
}

function ObjectTable({ rows }: { rows: JsonObject[] }) {
  if (!rows.length) return <EmptyState text="暂无可展示事实。" />;
  return (
    <div className="macro-decision__object-list">
      {rows.slice(0, 40).map((row, index) => (
        <article key={`${textValue(row.dataset_id) ?? textValue(row.contract_code) ?? index}`}>
          {Object.entries(row)
            .slice(0, 6)
            .map(([key, value]) => (
              <div key={key}>
                <span>{key}</span>
                <strong>{scalarLabel(value)}</strong>
              </div>
            ))}
        </article>
      ))}
    </div>
  );
}

function UnavailableCard({ label, reason }: { label: string; reason: string }) {
  return (
    <article className="macro-decision__unavailable">
      <span>UNAVAILABLE</span>
      <strong>{label}</strong>
      <p>{reasonLabel(reason)}</p>
    </article>
  );
}

function EmptyState({ text }: { text: string }) {
  return <p className="macro-decision__empty">{text}</p>;
}

function SourceLink({ href }: { href: string }) {
  return (
    <a href={href} rel="noreferrer" target="_blank">
      来源 <ExternalLink aria-hidden="true" />
    </a>
  );
}

function SignedCell({ value }: { value: number | null }) {
  return (
    <span
      data-sign={value == null ? "none" : value > 0 ? "up" : value < 0 ? "down" : "flat"}
      role="cell"
    >
      {formatSigned(value)}%
    </span>
  );
}

function formatNumber(value: number): string {
  return new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 }).format(value);
}

function formatSigned(value: number | null): string {
  if (value == null) return "—";
  return `${value > 0 ? "+" : ""}${formatNumber(value)}`;
}

function numberLabel(value: unknown): string {
  return typeof value === "number" && Number.isFinite(value) ? formatNumber(value) : "—";
}

function optionalPercent(value: number | undefined): string {
  return value == null ? "—" : `${formatNumber(value)}%`;
}

function scalarLabel(value: unknown): string {
  if (typeof value === "number") return formatNumber(value);
  if (typeof value === "string") return value;
  if (value == null) return "—";
  return JSON.stringify(value);
}

function textValue(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

function unitLabel(unit: string): string {
  return (
    {
      percent: "%",
      basis_points: "bp",
      index: "点",
      millions_usd: "百万美元",
      billions_usd: "十亿美元",
      usd_per_barrel: "美元/桶",
    }[unit] ?? unit
  );
}

function reasonLabel(reason: string): string {
  return (
    {
      licensed_contract_facts_not_configured: "尚无合规授权的底层期货合约事实，不生成近似概率。",
      licensed_security_level_facts_not_configured: "尚无合规的逐笔 TRACE 与 NAV 数据。",
      effective_dated_roster_not_ingested: "有效日期官员身份与投票事实尚未入库。",
      immutable_document_analysis_not_published: "尚未发布绑定原文哈希的不可变文件分析。",
    }[reason] ?? reason
  );
}

function windowLabel(value: MacroCurveSnapshot["window"]): string {
  return { current: "当前", "1w": "1周前", "1m": "1月前", "3m": "3月前" }[value];
}
