import * as PageState from "@shared/ui/PageState";
import { Button } from "@shared/ui/button";
import {
  ArrowDownRight,
  ArrowRight,
  ArrowUpRight,
  CircleMinus,
  Clock3,
  ExternalLink,
  RefreshCw,
  ShieldAlert,
} from "lucide-react";
import { Link } from "react-router-dom";

import {
  useMacroModuleQuery,
  useMacroOverviewQuery,
} from "../api/useMacroDecisionQuery";
import type {
  MacroAssetDirection,
  MacroChart,
  MacroChartPoint,
  MacroChange,
  MacroDailyJudgment,
  MacroModuleId,
  MacroModuleReadData,
  MacroOverviewReadData,
  MacroReadiness,
} from "../model/macroTypes";

import "./MacroDecisionPage.css";
import "./MacroDecisionEvidence.css";
import "./MacroDecisionPageResponsive.css";

const MODULE_ROUTES: ReadonlyArray<{
  id: MacroModuleId;
  path: string;
  label: string;
}> = [
  { id: "rates_fed", path: "/macro/rates-fed", label: "利率与美联储" },
  { id: "economy_inflation", path: "/macro/economy-inflation", label: "经济与通胀" },
  { id: "liquidity_funding", path: "/macro/liquidity-funding", label: "流动性与融资" },
  { id: "credit", path: "/macro/credit", label: "信用市场" },
  { id: "volatility", path: "/macro/volatility", label: "波动率" },
  { id: "cross_asset", path: "/macro/cross-asset", label: "大类资产与期货" },
];

export function MacroOverviewPage({ token }: { token: string }) {
  const query = useMacroOverviewQuery(token);
  if (query.isError && !query.data) {
    return <PageState.Error error={query.error} onRetry={() => void query.refetch()} />;
  }
  if (query.isLoading || !query.data) {
    return <PageState.Loading label="加载每日宏观决策台" layout="route" rows={8} />;
  }
  return (
    <PageState.Stale updating={query.isFetching && !query.isLoading}>
      <main aria-label="每日宏观决策台" className="macro-decision" data-page-archetype="decision">
        <DecisionHeader
          isFetching={query.isFetching}
          latestFactAtMs={query.data.latest_fact_at_ms}
          judgmentCutoffMs={query.data.judgment_cutoff_ms}
          readiness={query.data.overall_readiness}
          title="每日宏观决策台"
          onRefresh={() => void query.refetch()}
        />
        <MacroNavigation />
        <Overview data={query.data} />
      </main>
    </PageState.Stale>
  );
}

export function MacroModulePage({
  token,
  moduleId,
}: {
  token: string;
  moduleId: MacroModuleId;
}) {
  const query = useMacroModuleQuery(token, moduleId);
  if (query.isError && !query.data) {
    return <PageState.Error error={query.error} onRetry={() => void query.refetch()} />;
  }
  if (query.isLoading || !query.data) {
    return <PageState.Loading label="加载宏观模块" layout="route" rows={8} />;
  }
  return (
    <PageState.Stale updating={query.isFetching && !query.isLoading}>
      <main
        aria-label={query.data.label}
        className="macro-decision"
        data-page-archetype="decision"
      >
        <DecisionHeader
          isFetching={query.isFetching}
          latestFactAtMs={query.data.latest_fact_at_ms}
          judgmentCutoffMs={query.data.judgment_cutoff_ms}
          readiness={query.data.readiness}
          title={query.data.label}
          onRefresh={() => void query.refetch()}
        />
        <MacroNavigation activeModule={moduleId} />
        <ModuleDetail module={query.data} />
      </main>
    </PageState.Stale>
  );
}

function DecisionHeader({
  title,
  readiness,
  judgmentCutoffMs,
  latestFactAtMs,
  isFetching,
  onRefresh,
}: {
  title: string;
  readiness: MacroReadiness;
  judgmentCutoffMs: number | null;
  latestFactAtMs: number;
  isFetching: boolean;
  onRefresh: () => void;
}) {
  return (
    <header className="macro-decision__header">
      <div>
        <span>DAILY MACRO DECISION WORKBENCH</span>
        <h1>{title}</h1>
        <p>事实、定价、矛盾与下一检查点分开呈现；不使用一个总分替代判断。</p>
      </div>
      <ReadinessBadge readiness={readiness} />
      <dl>
        <div>
          <dt>判断截点</dt>
          <dd>{formatInstant(judgmentCutoffMs)}</dd>
        </div>
        <div>
          <dt>最新事实</dt>
          <dd>{formatInstant(latestFactAtMs || null)}</dd>
        </div>
      </dl>
      <Button disabled={isFetching} onClick={onRefresh} size="sm" type="button" variant="outline">
        <RefreshCw aria-hidden="true" />
        {isFetching ? "刷新中" : "刷新"}
      </Button>
    </header>
  );
}

function MacroNavigation({ activeModule }: { activeModule?: MacroModuleId }) {
  return (
    <nav aria-label="宏观决策模块" className="macro-decision__nav">
      <Link aria-current={activeModule ? undefined : "page"} to="/macro">
        决策总览
      </Link>
      {MODULE_ROUTES.map((route) => (
        <Link
          aria-current={activeModule === route.id ? "page" : undefined}
          key={route.id}
          to={route.path}
        >
          {route.label}
        </Link>
      ))}
      <Link to="/macro/research">深度研究</Link>
    </nav>
  );
}

function Overview({ data }: { data: MacroOverviewReadData }) {
  return (
    <>
      {data.daily_judgment ? (
        <JudgmentPanel judgment={data.daily_judgment} />
      ) : (
        <section className="macro-decision__notice">
          <ShieldAlert aria-hidden="true" />
          <div>
            <h2>今日判断尚未发布</h2>
            <p>关键模块未就绪时系统保留事实页面，但不会生成新的宏观判断。</p>
          </div>
        </section>
      )}

      <section aria-label="六个宏观模块" className="macro-decision__module-grid">
        {data.modules.map((module) => (
          <article className="macro-decision__module-card" key={module.module_id}>
            <header>
              <ReadinessBadge readiness={module.readiness === "missing" ? "blocked" : module.readiness} />
              <small>{module.gap_count} 个缺口</small>
            </header>
            <h2>{module.label}</h2>
            <p>{textValue(module.current_state?.interpretation) ?? "模块正在首次构建。"}</p>
            <ChangeList changes={module.top_changes.slice(0, 2)} compact />
            <Link to={module.href}>
              打开模块
              <ArrowRight aria-hidden="true" />
            </Link>
          </article>
        ))}
      </section>

      <ResearchStrip research={data.research} />
    </>
  );
}

function JudgmentPanel({ judgment }: { judgment: MacroDailyJudgment }) {
  return (
    <section className="macro-decision__judgment">
      <header>
        <div>
          <span>DAILY JUDGMENT · {judgment.session_date}</span>
          <h2>{judgment.overall_state}</h2>
        </div>
        <small>证据截点与最新事实独立记录</small>
      </header>
      <div className="macro-decision__dimensions">
        {Object.entries(judgment.dimensions).map(([dimension, state]) => (
          <article key={dimension}>
            <span>{dimensionLabel(dimension)}</span>
            <strong>{state.state}</strong>
            <p>{state.driver}</p>
          </article>
        ))}
      </div>
      <JudgmentEvidence judgment={judgment} />
      <div className="macro-decision__judgment-grid">
        <div>
          <h3>主导压力</h3>
          <ul>
            {judgment.dominant_pressures.length ? (
              judgment.dominant_pressures.map((pressure, index) => (
                <li key={index}>{textValue(pressure.driver) ?? "等待证据"}</li>
              ))
            ) : (
              <li>当前没有三项以上压力共振。</li>
            )}
          </ul>
        </div>
        <AssetDirections directions={judgment.asset_directions} />
      </div>
    </section>
  );
}

function JudgmentEvidence({ judgment }: { judgment: MacroDailyJudgment }) {
  const falsifiers = judgment.falsifiers.flatMap((entry) =>
    stringArray(entry.items).map((item) => `${moduleLabel(textValue(entry.module_id))}：${item}`),
  );
  return (
    <div className="macro-decision__decision-evidence">
      <JudgmentEvidenceCard
        empty="尚无足够历史确认变化。"
        items={judgment.top_3_changes.map((item) => {
          const label = textValue(item.label) ?? textValue(item.dataset_id) ?? "宏观事实";
          const value = numberValue(item.value);
          const change = numberValue(item.short_change);
          return `${label}：${value == null ? "—" : formatNumber(value)} ${unitLabel(textValue(item.unit) ?? "")}；短窗 ${formatSigned(change)}`;
        })}
        title="今日最重要变化"
      />
      <JudgmentEvidenceCard
        empty="当前没有识别到结构性矛盾。"
        items={judgment.contradictions.map(
          (item) =>
            `${moduleLabel(textValue(item.module_id))}：${textValue(item.text) ?? "等待更多证据"}`,
        )}
        title="矛盾与反证"
      />
      <JudgmentEvidenceCard
        empty="暂无预设失效条件。"
        items={falsifiers}
        title="判断失效条件"
      />
      <JudgmentEvidenceCard
        empty="当前没有待补检查点。"
        items={judgment.next_checkpoints.map(
          (item) =>
            `${textValue(item.label) ?? textValue(item.dataset_id) ?? moduleLabel(textValue(item.module_id))}：${textValue(item.next_check) ?? "按数据时钟检查"}`,
        )}
        title="下一检查点"
      />
      <JudgmentEvidenceCard
        empty="关键数据集均在新鲜度预算内。"
        items={judgment.gaps.map(
          (item) =>
            `${textValue(item.label) ?? textValue(item.dataset_id) ?? "数据集"}：${gapReason(textValue(item.reason))}`,
        )}
        title={`数据缺口 · ${judgment.citations.length} 条可追溯事实`}
      />
    </div>
  );
}

function JudgmentEvidenceCard({
  title,
  items,
  empty,
}: {
  title: string;
  items: string[];
  empty: string;
}) {
  return (
    <article>
      <h3>{title}</h3>
      <ul>{items.length ? items.slice(0, 6).map((item) => <li key={item}>{item}</li>) : <li>{empty}</li>}</ul>
    </article>
  );
}

function AssetDirections({ directions }: { directions: Record<string, MacroAssetDirection> }) {
  return (
    <div className="macro-decision__assets">
      <h3>固定资产方向</h3>
      <div role="table">
        <div role="row">
          <span role="columnheader">资产</span>
          <span role="columnheader">1周</span>
          <span role="columnheader">1月</span>
          <span role="columnheader">置信度</span>
        </div>
        {Object.entries(directions).map(([asset, direction]) => (
          <div key={asset} role="row">
            <strong role="cell">{asset}</strong>
            <Direction value={direction["1w"]} />
            <Direction value={direction["1m"]} />
            <span role="cell">{direction.confidence}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

function Direction({ value }: { value: MacroAssetDirection["1w"] }) {
  const Icon =
    value === "up"
      ? ArrowUpRight
      : value === "down"
        ? ArrowDownRight
        : value === "range"
          ? CircleMinus
          : Clock3;
  return (
    <span data-direction={value} role="cell">
      <Icon aria-hidden="true" />
      {value}
    </span>
  );
}

function ModuleDetail({ module }: { module: MacroModuleReadData }) {
  return (
    <>
      <section className="macro-decision__state">
        <div>
          <span>CURRENT STATE</span>
          <h2>{module.current_state.headline}</h2>
          <p>{module.current_state.interpretation}</p>
        </div>
        <div>
          <span>最重要变化</span>
          <ChangeList changes={module.top_changes.slice(0, 3)} />
        </div>
      </section>

      <section aria-label="模块图表" className="macro-decision__charts">
        {module.charts.map((chart) => (
          <MacroChartFigure chart={chart} key={chart.chart_id} />
        ))}
      </section>

      <section className="macro-decision__review-grid">
        <ReviewList title="矛盾与反证" items={module.contradictions} empty="暂未识别结构性矛盾。" />
        <ReviewList title="判断失效条件" items={module.falsifiers} empty="暂无预设失效条件。" />
        <CheckpointList items={module.next_checkpoints} />
        <GapList module={module} />
      </section>

      <details className="macro-decision__evidence">
        <summary>展开原始证据与 Dataset 状态</summary>
        <div className="macro-decision__dataset-list">
          {module.dataset_states.map((dataset) => (
            <article data-state={dataset.state} key={dataset.dataset_id}>
              <div>
                <strong>{dataset.label}</strong>
                <small>{dataset.dataset_id}</small>
              </div>
              <span>{dataset.state}</span>
              <time>{dataset.latest_reference ?? "尚无事实"}</time>
              <a href={dataset.source_url} rel="noreferrer" target="_blank">
                来源
                <ExternalLink aria-hidden="true" />
              </a>
            </article>
          ))}
        </div>
        <RawEvidence module={module} />
      </details>
    </>
  );
}

function ChangeList({ changes, compact = false }: { changes: MacroChange[]; compact?: boolean }) {
  if (!changes.length) return <p className="macro-decision__muted">尚无足够历史计算变化。</p>;
  return (
    <div className="macro-decision__changes" data-compact={compact || undefined}>
      {changes.map((change) => (
        <article key={change.dataset_id}>
          <span>{change.label}</span>
          <strong>
            {formatNumber(change.value)} {unitLabel(change.unit)}
          </strong>
          <small>
            {change.short_window} {formatSigned(change.short_change)} · {change.medium_window}{" "}
            {formatSigned(change.medium_change)}
          </small>
        </article>
      ))}
    </div>
  );
}

function MacroChartFigure({ chart }: { chart: MacroChart }) {
  const rows = chart.series
    .map((datasetId) => ({
      datasetId,
      points: chart.points.filter((point) => point.dataset_id === datasetId),
    }))
    .filter((row) => row.points.length);
  return (
    <figure className="macro-decision__chart">
      <figcaption>
        <h2>{chart.title}</h2>
        <span>各序列独立刻度；窗口由数据频率定义</span>
      </figcaption>
      {rows.length ? (
        rows.map((row) => <Sparkline datasetId={row.datasetId} key={row.datasetId} points={row.points} />)
      ) : (
        <p>等待有效历史数据。</p>
      )}
    </figure>
  );
}

function Sparkline({ datasetId, points }: { datasetId: string; points: MacroChartPoint[] }) {
  const sampled = downsample(points, 120);
  const values = sampled.map((point) => point.y);
  const minimum = Math.min(...values);
  const maximum = Math.max(...values);
  const span = maximum - minimum || 1;
  const path = sampled
    .map((point, index) => {
      const x = sampled.length === 1 ? 50 : (index / (sampled.length - 1)) * 100;
      const y = 38 - ((point.y - minimum) / span) * 34;
      return `${index ? "L" : "M"} ${x.toFixed(2)} ${y.toFixed(2)}`;
    })
    .join(" ");
  const latest = sampled.at(-1);
  const label = latest?.label ?? datasetId;
  return (
    <div className="macro-decision__sparkline">
      <div>
        <strong>{label}</strong>
        <span>
          {latest ? `${formatNumber(latest.y)} ${unitLabel(latest.unit)}` : "—"}
        </span>
      </div>
      <svg aria-label={`${label} 历史走势`} preserveAspectRatio="none" viewBox="0 0 100 40">
        <path d={path} />
      </svg>
      <small>
        {sampled[0]?.x} → {latest?.x}
      </small>
    </div>
  );
}

function RawEvidence({ module }: { module: MacroModuleReadData }) {
  const labels = new Map(module.dataset_states.map((item) => [item.dataset_id, item.label]));
  return (
    <section className="macro-decision__raw-evidence">
      <h3>当前原始事实</h3>
      <div role="table">
        <div role="row">
          <span role="columnheader">数据 / 合约</span>
          <span role="columnheader">参考期</span>
          <span role="columnheader">值</span>
          <span role="columnheader">可用时间</span>
          <span role="columnheader">来源</span>
        </div>
        {module.raw_evidence.length ? (
          module.raw_evidence.map((fact, index) => {
            const datasetId = textValue(fact.dataset_id) ?? "unknown";
            const label = textValue(fact.label) ?? labels.get(datasetId) ?? datasetId;
            const value = fact.value;
            const unit = textValue(fact.unit) ?? "";
            const receivedAt = numberValue(fact.received_at_ms);
            const sourceUrl = textValue(fact.source_url);
            return (
              <div key={textValue(fact.fact_ref) ?? `${datasetId}-${index}`} role="row">
                <span role="cell">
                  <strong>{label}</strong>
                  <small>{datasetId}</small>
                </span>
                <time role="cell">{textValue(fact.reference) ?? "—"}</time>
                <span role="cell">
                  {typeof value === "number"
                    ? `${formatNumber(value)} ${unitLabel(unit)}`
                    : textValue(value) ?? "—"}
                </span>
                <time role="cell">{formatInstant(receivedAt)}</time>
                <span role="cell">
                  {sourceUrl ? (
                    <a href={sourceUrl} rel="noreferrer" target="_blank">
                      打开
                      <ExternalLink aria-hidden="true" />
                    </a>
                  ) : (
                    "—"
                  )}
                </span>
              </div>
            );
          })
        ) : (
          <p>尚无有效事实。</p>
        )}
      </div>
    </section>
  );
}

function ReviewList({ title, items, empty }: { title: string; items: string[]; empty: string }) {
  return (
    <article>
      <h2>{title}</h2>
      <ul>{items.length ? items.map((item) => <li key={item}>{item}</li>) : <li>{empty}</li>}</ul>
    </article>
  );
}

function CheckpointList({ items }: { items: Record<string, unknown>[] }) {
  return (
    <article>
      <h2>下一检查点</h2>
      <ul>
        {items.length ? (
          items.map((item, index) => (
            <li key={index}>
              {textValue(item.label) ?? textValue(item.dataset_id) ?? "数据检查"}：
              {textValue(item.next_check) ?? "按数据时钟检查"}
            </li>
          ))
        ) : (
          <li>当前没有待补检查点。</li>
        )}
      </ul>
    </article>
  );
}

function GapList({ module }: { module: MacroModuleReadData }) {
  return (
    <article>
      <h2>数据缺口</h2>
      <ul>
        {module.gaps.length ? (
          module.gaps.map((gap, index) => (
            <li key={index}>
              {textValue(gap.label) ?? "数据集"}：{gapReason(textValue(gap.reason))}
            </li>
          ))
        ) : (
          <li>关键数据集均在当前新鲜度预算内。</li>
        )}
      </ul>
    </article>
  );
}

function ResearchStrip({ research }: { research: MacroOverviewReadData["research"] }) {
  return (
    <section className="macro-decision__research">
      <div>
        <span>ASYNC DEEP RESEARCH</span>
        <h2>{research.title ?? researchStateLabel(research.state)}</h2>
        <p>{research.executive_summary ?? "研究链独立运行，失败不会影响事实与每日判断。"}</p>
      </div>
      <dl>
        <div>
          <dt>Evidence Pack</dt>
          <dd>{research.evidence_pack_id ?? "尚未绑定"}</dd>
        </div>
        <div>
          <dt>Reviewer</dt>
          <dd>{research.reviewer_disposition ?? "尚未审阅"}</dd>
        </div>
      </dl>
      <Link to={research.href}>
        查看深度研究
        <ArrowRight aria-hidden="true" />
      </Link>
    </section>
  );
}

function ReadinessBadge({ readiness }: { readiness: MacroReadiness }) {
  return (
    <span className="macro-decision__readiness" data-readiness={readiness}>
      {readinessLabel(readiness)}
    </span>
  );
}

function readinessLabel(readiness: MacroReadiness) {
  return { ready: "就绪", degraded: "降级可读", blocked: "阻止新判断" }[readiness];
}

function researchStateLabel(state: MacroOverviewReadData["research"]["state"]) {
  return { current: "研究已发布", generating: "研究生成中", failed: "研究失败", missing: "尚无研究" }[
    state
  ];
}

function dimensionLabel(value: string) {
  return {
    growth: "增长",
    inflation: "通胀",
    policy: "政策",
    liquidity: "流动性",
    credit: "信用",
    volatility: "波动率",
  }[value] ?? value;
}

function gapReason(value: string | null) {
  return (
    {
      licensed_data_not_configured: "免费阶段没有合规授权数据",
      no_valid_fact: "尚无有效事实",
      fact_past_freshness_budget: "超过新鲜度预算",
      fact_delayed: "数据延迟",
      module_projection_missing: "模块投影正在初始化",
    }[value ?? ""] ??
    value ??
    "原因待确认"
  );
}

function textValue(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

function numberValue(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function stringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

function moduleLabel(value: string | null): string {
  return (
    {
      rates_fed: "利率与美联储",
      economy_inflation: "经济与通胀",
      liquidity_funding: "流动性与融资",
      credit: "信用市场",
      volatility: "波动率",
      cross_asset: "大类资产与期货",
    }[value ?? ""] ?? value ?? "宏观模块"
  );
}

function formatInstant(value: number | null): string {
  if (!value) return "尚未发布";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date(value));
}

function formatNumber(value: number): string {
  return new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 }).format(value);
}

function formatSigned(value: number | null): string {
  if (value == null) return "—";
  return `${value > 0 ? "+" : ""}${formatNumber(value)}`;
}

function unitLabel(unit: string) {
  return {
    percent: "%",
    basis_points: "bp",
    billions_usd: "十亿美元",
    millions_usd: "百万美元",
    index: "点",
    index_points: "点",
    percent_open_interest: "% OI",
    price: "",
    usdt: "USDT",
    persons: "人",
    thousands_persons: "千人",
  }[unit] ?? unit;
}

function downsample(points: MacroChartPoint[], maximum: number): MacroChartPoint[] {
  if (points.length <= maximum) return points;
  const step = (points.length - 1) / (maximum - 1);
  return Array.from({ length: maximum }, (_, index) => points[Math.round(index * step)]);
}
