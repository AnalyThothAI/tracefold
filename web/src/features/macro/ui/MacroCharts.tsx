import {
  ColorType,
  CrosshairMode,
  LineSeries,
  LineStyle,
  createChart,
  type ISeriesApi,
  type LineData,
  type Time,
} from "lightweight-charts";
import { useEffect, useMemo, useRef, useState } from "react";
import type { CSSProperties } from "react";

import type {
  MacroCorrelationView,
  MacroCurveSnapshotView,
  MacroHistoryPoint,
  MacroIndicatorView,
  MacroReleaseView,
} from "../model/macroViewModels";

import "./MacroCharts.css";

export type MacroChartSeries = {
  id: string;
  label: string;
  color?: string;
  points: MacroHistoryPoint[];
  lineStyle?: "solid" | "dashed" | "dotted";
};

export type MacroBarGroup = {
  id: string;
  label: string;
  values: Array<{
    id: string;
    label: string;
    value: number | null;
    color?: string;
  }>;
};

const SERIES_COLORS = [
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
];

export function MacroTimeSeriesChart({
  title,
  description,
  series,
  unit,
  baseline,
  defaultRange = "natural",
}: {
  title: string;
  description?: string;
  series: MacroChartSeries[];
  unit?: string;
  baseline?: number;
  defaultRange?: "natural" | "all";
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const pinnedRef = useRef(false);
  const hoverTextRef = useRef("");
  const [range, setRange] = useState<"natural" | "all">(defaultRange);
  const [hiddenSeries, setHiddenSeries] = useState<Set<string>>(() => new Set());
  const [readout, setReadout] = useState("移动十字线查看精确读数；点击图表可锁定。");
  const [pinned, setPinned] = useState(false);
  const visibleSeries = useMemo(
    () =>
      series
        .filter((item) => !hiddenSeries.has(item.id))
        .map((item) => ({
          ...item,
          points: range === "natural" ? naturalWindow(item.points) : item.points,
        })),
    [hiddenSeries, range, series],
  );
  const hasPoints = series.some((item) => item.points.length);

  useEffect(() => {
    const container = containerRef.current;
    if (!container || !hasPoints || isTestDom()) return;
    const chart = createChart(container, {
      autoSize: true,
      height: 286,
      layout: {
        background: { color: "transparent", type: ColorType.Solid },
        attributionLogo: false,
        textColor: "#8ea0b8",
        fontFamily: "var(--font-mono)",
        fontSize: 11,
      },
      grid: {
        horzLines: { color: "rgba(138, 157, 181, 0.12)" },
        vertLines: { color: "rgba(138, 157, 181, 0.08)" },
      },
      crosshair: {
        mode: CrosshairMode.Normal,
        horzLine: { color: "#8ea0b8", labelBackgroundColor: "#26364b" },
        vertLine: { color: "#8ea0b8", labelBackgroundColor: "#26364b" },
      },
      rightPriceScale: {
        borderColor: "rgba(138, 157, 181, 0.2)",
        scaleMargins: { bottom: 0.12, top: 0.12 },
      },
      timeScale: {
        borderColor: "rgba(138, 157, 181, 0.2)",
        rightOffset: 3,
        timeVisible: false,
      },
      localization: {
        priceFormatter: (value: number) => `${formatNumber(value)}${unitLabel(unit)}`,
      },
    });
    const chartSeries: Array<{
      api: ISeriesApi<"Line">;
      label: string;
    }> = [];
    visibleSeries.forEach((item, index) => {
      const api = chart.addSeries(LineSeries, {
        color: item.color ?? SERIES_COLORS[index % SERIES_COLORS.length],
        lineWidth: 2,
        lineStyle: chartLineStyle(item.lineStyle),
        priceLineVisible: false,
        lastValueVisible: true,
        title: item.label,
      });
      api.setData(
        item.points.map(
          (point): LineData<Time> => ({
            time: point.date as Time,
            value: point.value,
          }),
        ),
      );
      chartSeries.push({ api, label: item.label });
    });
    if (baseline != null && chartSeries[0]) {
      chartSeries[0].api.createPriceLine({
        axisLabelVisible: true,
        color: "#77869b",
        lineStyle: LineStyle.Dashed,
        lineWidth: 1,
        price: baseline,
        title: `基准 ${formatNumber(baseline)}`,
      });
    }
    chart.subscribeCrosshairMove((parameter) => {
      if (!parameter.time) return;
      const values = chartSeries
        .map(({ api, label }) => {
          const datum = parameter.seriesData.get(api);
          return datum && "value" in datum
            ? `${label} ${formatNumber(Number(datum.value))}${unitLabel(unit)}`
            : null;
        })
        .filter((value): value is string => value != null);
      hoverTextRef.current = `${String(parameter.time)} · ${values.join(" · ")}`;
      if (!pinnedRef.current) setReadout(hoverTextRef.current);
    });
    chart.timeScale().fitContent();
    return () => chart.remove();
  }, [baseline, hasPoints, unit, visibleSeries]);

  function toggleSeries(id: string) {
    setHiddenSeries((current) => {
      const next = new Set(current);
      if (next.has(id)) {
        next.delete(id);
      } else if (current.size < series.length - 1) {
        next.add(id);
      }
      return next;
    });
  }

  function togglePinned() {
    pinnedRef.current = !pinnedRef.current;
    setPinned(pinnedRef.current);
    if (!pinnedRef.current && hoverTextRef.current) setReadout(hoverTextRef.current);
  }

  return (
    <figure className="macro-chart">
      <figcaption>
        <div>
          <strong>{title}</strong>
          {description ? <span>{description}</span> : null}
        </div>
        <div aria-label={`${title}时间范围`} className="macro-chart__range">
          <button
            aria-pressed={range === "natural"}
            onClick={() => setRange("natural")}
            type="button"
          >
            自然窗口
          </button>
          <button aria-pressed={range === "all"} onClick={() => setRange("all")} type="button">
            全部历史
          </button>
        </div>
      </figcaption>
      <div aria-label={`${title}图例`} className="macro-chart__legend">
        {series.map((item, index) => (
          <button
            aria-pressed={!hiddenSeries.has(item.id)}
            key={item.id}
            onClick={() => toggleSeries(item.id)}
            style={
              {
                "--series-color": item.color ?? SERIES_COLORS[index % SERIES_COLORS.length],
              } as CSSProperties
            }
            type="button"
          >
            {item.label}
          </button>
        ))}
      </div>
      {hasPoints ? (
        <>
          <div
            aria-label={`${title}，点击锁定读数`}
            className="macro-chart__canvas"
            onClick={togglePinned}
            onKeyDown={(event) => {
              if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                togglePinned();
              }
            }}
            ref={containerRef}
            role="button"
            tabIndex={0}
          />
          <p aria-live="polite" className="macro-chart__readout" data-pinned={pinned || undefined}>
            {pinned ? "已锁定 · " : ""}
            {readout}
          </p>
        </>
      ) : (
        <p className="macro-chart__empty">该图所需历史序列尚未回填。</p>
      )}
      <CompactSeriesTable series={series} unit={unit} />
    </figure>
  );
}

export function MacroCurveChart({
  title,
  snapshots,
  unit = "%",
}: {
  title: string;
  snapshots: MacroCurveSnapshotView[];
  unit?: string;
}) {
  const values = snapshots.flatMap((snapshot) => snapshot.points.map((point) => point.value));
  if (!values.length) {
    return <EmptyChart title={title} />;
  }
  const minimum = Math.min(...values);
  const maximum = Math.max(...values);
  const span = maximum - minimum || 1;
  const pointSpan = Math.max(...snapshots.map((snapshot) => snapshot.points.length - 1), 1);
  return (
    <figure className="macro-chart macro-chart--curve">
      <figcaption>
        <strong>{title}</strong>
        <span>同一期限坐标比较当前、1周、1月、3月</span>
      </figcaption>
      <svg aria-label={title} role="img" viewBox="0 0 720 300">
        {[0, 1, 2, 3, 4].map((index) => {
          const y = 24 + index * 58;
          const label = maximum - (span * index) / 4;
          return (
            <g key={index}>
              <line className="macro-chart__grid-line" x1="58" x2="700" y1={y} y2={y} />
              <text className="macro-chart__axis-label" x="6" y={y + 4}>
                {formatNumber(label)}
                {unit}
              </text>
            </g>
          );
        })}
        {snapshots.map((snapshot, index) => (
          <path
            className="macro-chart__curve-line"
            d={snapshot.points
              .map((point, pointIndex) => {
                const x = 58 + (pointIndex / pointSpan) * 642;
                const y = 256 - ((point.value - minimum) / span) * 232;
                return `${pointIndex ? "L" : "M"} ${x.toFixed(2)} ${y.toFixed(2)}`;
              })
              .join(" ")}
            data-window={snapshot.window}
            key={snapshot.window}
            style={{ "--series-color": SERIES_COLORS[index] } as CSSProperties}
          />
        ))}
        {snapshots[0]?.points.map((point, pointIndex) => {
          const x = 58 + (pointIndex / pointSpan) * 642;
          return (
            <text
              className="macro-chart__axis-label"
              key={point.tenor}
              textAnchor="middle"
              x={x}
              y="284"
            >
              {point.tenor}
            </text>
          );
        })}
      </svg>
      <div className="macro-chart__legend">
        {snapshots.map((snapshot, index) => (
          <span
            key={snapshot.window}
            style={{ "--series-color": SERIES_COLORS[index] } as CSSProperties}
          >
            {windowLabel(snapshot.window)} · {snapshot.asOf}
          </span>
        ))}
      </div>
      <details className="macro-chart__data">
        <summary>查看期限数据</summary>
        <div className="macro-chart__table" role="table">
          <div role="row">
            <span role="columnheader">窗口</span>
            <span role="columnheader">截至</span>
            <span role="columnheader">期限与数值</span>
          </div>
          {snapshots.map((snapshot) => (
            <div key={snapshot.window} role="row">
              <span role="cell">{windowLabel(snapshot.window)}</span>
              <span role="cell">{snapshot.asOf}</span>
              <span role="cell">
                {snapshot.points
                  .map((point) => `${point.tenor} ${formatNumber(point.value)}${unit}`)
                  .join(" · ")}
              </span>
            </div>
          ))}
        </div>
      </details>
    </figure>
  );
}

export function MacroBarChart({
  title,
  description,
  groups,
  unit,
  baseline = 0,
}: {
  title: string;
  description?: string;
  groups: MacroBarGroup[];
  unit?: string;
  baseline?: number;
}) {
  const numericValues = groups.flatMap((group) =>
    group.values.flatMap((item) => (item.value == null ? [] : [item.value])),
  );
  if (!numericValues.length) return <EmptyChart title={title} />;
  const minimum = Math.min(baseline, ...numericValues);
  const maximum = Math.max(baseline, ...numericValues);
  const span = maximum - minimum || 1;
  const baselineY = 244 - ((baseline - minimum) / span) * 204;
  const groupWidth = 620 / Math.max(groups.length, 1);
  return (
    <figure className="macro-chart macro-chart--bars">
      <figcaption>
        <div>
          <strong>{title}</strong>
          {description ? <span>{description}</span> : null}
        </div>
      </figcaption>
      <svg aria-label={title} role="img" viewBox="0 0 720 290">
        <line className="macro-chart__baseline" x1="76" x2="696" y1={baselineY} y2={baselineY} />
        {groups.map((group, groupIndex) => {
          const barWidth = Math.min(28, (groupWidth - 18) / Math.max(group.values.length, 1));
          return (
            <g key={group.id}>
              {group.values.map((item, itemIndex) => {
                if (item.value == null) return null;
                const x = 76 + groupIndex * groupWidth + 9 + itemIndex * barWidth;
                const valueY = 244 - ((item.value - minimum) / span) * 204;
                const y = Math.min(valueY, baselineY);
                const height = Math.max(Math.abs(valueY - baselineY), 1);
                return (
                  <rect
                    className="macro-chart__bar"
                    height={height}
                    key={item.id}
                    style={
                      {
                        "--series-color":
                          item.color ?? SERIES_COLORS[itemIndex % SERIES_COLORS.length],
                      } as CSSProperties
                    }
                    width={Math.max(barWidth - 4, 5)}
                    x={x}
                    y={y}
                  />
                );
              })}
              <text
                className="macro-chart__axis-label"
                textAnchor="middle"
                x={76 + groupIndex * groupWidth + groupWidth / 2}
                y="272"
              >
                {truncate(group.label, 14)}
              </text>
            </g>
          );
        })}
      </svg>
      <div className="macro-chart__legend">
        {[
          ...new Map(
            groups.flatMap((group) => group.values).map((item) => [item.id, item]),
          ).values(),
        ].map((item, index) => (
          <span
            key={item.id}
            style={
              {
                "--series-color": item.color ?? SERIES_COLORS[index % SERIES_COLORS.length],
              } as CSSProperties
            }
          >
            {item.label}
          </span>
        ))}
      </div>
      <details className="macro-chart__data">
        <summary>查看柱图数据</summary>
        <div className="macro-chart__table" role="table">
          <div role="row">
            <span role="columnheader">分组</span>
            <span role="columnheader">系列</span>
            <span role="columnheader">数值</span>
          </div>
          {groups.flatMap((group) =>
            group.values.map((item) => (
              <div key={`${group.id}:${item.id}`} role="row">
                <span role="cell">{group.label}</span>
                <span role="cell">{item.label}</span>
                <span role="cell">
                  {item.value == null ? "—" : `${formatNumber(item.value)}${unitLabel(unit)}`}
                </span>
              </div>
            )),
          )}
        </div>
      </details>
    </figure>
  );
}

export function MacroCorrelationHeatmap({
  title,
  rows,
}: {
  title: string;
  rows: MacroCorrelationView[];
}) {
  if (!rows.length) return <EmptyChart title={title} />;
  const symbols = [...new Set(rows.flatMap((row) => [row.left, row.right]))].sort();
  const lookup = new Map<string, MacroCorrelationView>();
  rows.forEach((row) => {
    lookup.set(`${row.left}:${row.right}`, row);
    lookup.set(`${row.right}:${row.left}`, row);
  });
  return (
    <figure className="macro-chart macro-chart--heatmap">
      <figcaption>
        <strong>{title}</strong>
        <span>最多 120 个共同日收益样本；对角线固定为 1。</span>
      </figcaption>
      <div
        className="macro-chart__heatmap"
        style={{ "--heatmap-columns": symbols.length + 1 } as CSSProperties}
      >
        <span aria-hidden="true" />
        {symbols.map((symbol) => (
          <strong key={`head:${symbol}`}>{symbol}</strong>
        ))}
        {symbols.flatMap((left) => [
          <strong key={`row:${left}`}>{left}</strong>,
          ...symbols.map((right) => {
            const row = lookup.get(`${left}:${right}`);
            const value = left === right ? 1 : (row?.correlation ?? null);
            return (
              <span
                aria-label={`${left} 与 ${right} 相关性 ${value == null ? "不可用" : value.toFixed(2)}`}
                data-empty={value == null || undefined}
                key={`${left}:${right}`}
                style={
                  value == null
                    ? undefined
                    : ({
                        "--heat-alpha": Math.max(Math.abs(value), 0.08),
                        "--heat-color": value >= 0 ? "103, 212, 255" : "255, 124, 139",
                      } as CSSProperties)
                }
                title={`${left}/${right}: ${value == null ? "—" : value.toFixed(2)}${
                  row?.sampleCount ? ` (n=${row.sampleCount})` : ""
                }`}
              >
                {value == null ? "—" : value.toFixed(2)}
              </span>
            );
          }),
        ])}
      </div>
    </figure>
  );
}

export function MacroIndicatorTable({
  indicators,
  title = "结构化指标数据",
}: {
  indicators: MacroIndicatorView[];
  title?: string;
}) {
  if (!indicators.length) return <p className="macro-chart__empty">当前没有可用指标。</p>;
  return (
    <details className="macro-chart__data">
      <summary>
        {title}（{indicators.length}）
      </summary>
      <div className="macro-chart__table macro-chart__table--indicators" role="table">
        <div role="row">
          <span role="columnheader">指标</span>
          <span role="columnheader">最新</span>
          <span role="columnheader">1周</span>
          <span role="columnheader">1月</span>
          <span role="columnheader">样本/分位</span>
          <span role="columnheader">截至</span>
        </div>
        {indicators.map((item) => (
          <div key={item.datasetId} role="row">
            <span role="cell">
              <strong>{item.label}</strong>
              <small>{item.datasetId}</small>
            </span>
            <span role="cell">{formatOptional(item.latestValue, item.unit)}</span>
            <SignedValue value={item.change1w} />
            <SignedValue value={item.change1m} />
            <span role="cell">
              {item.sampleCount ?? "—"} /{" "}
              {item.percentile == null ? "—" : `${formatNumber(item.percentile)}%`}
            </span>
            <span role="cell">
              {item.asOf ?? "—"}
              {item.sourceUrl ? (
                <a href={item.sourceUrl} rel="noreferrer" target="_blank">
                  来源
                </a>
              ) : null}
            </span>
          </div>
        ))}
      </div>
    </details>
  );
}

export function MacroReleaseStrip({ releases }: { releases: MacroReleaseView[] }) {
  if (!releases.length) return null;
  return (
    <section aria-label="官方发布事实" className="macro-chart__release-strip">
      {releases.map((release) => (
        <article key={release.datasetId}>
          <span>
            {release.label} · {release.referencePeriod ?? "当前期"}
          </span>
          <strong>{formatOptional(release.actualValue, release.unit)}</strong>
          <small>
            {release.estimateValue == null
              ? "未提供一致预期"
              : `预期 ${formatOptional(release.estimateValue, release.unit)}`}{" "}
            · 前值 {formatOptional(release.priorValue, release.unit)}
          </small>
          <small>
            {release.estimateValue != null && release.surprise != null
              ? `surprise ${formatSigned(release.surprise)} · `
              : null}
            revision {formatSigned(release.revision)}
          </small>
          {release.sourceUrl ? (
            <a href={release.sourceUrl} rel="noreferrer" target="_blank">
              官方来源
            </a>
          ) : null}
        </article>
      ))}
    </section>
  );
}

function CompactSeriesTable({ series, unit }: { series: MacroChartSeries[]; unit?: string }) {
  const rows = series.flatMap((item) =>
    item.points.slice(-6).map((point) => ({
      key: `${item.id}:${point.date}`,
      series: item.label,
      date: point.date,
      value: point.value,
    })),
  );
  if (!rows.length) return null;
  return (
    <details className="macro-chart__data">
      <summary>查看最近数据</summary>
      <div className="macro-chart__table" role="table">
        <div role="row">
          <span role="columnheader">系列</span>
          <span role="columnheader">日期</span>
          <span role="columnheader">数值</span>
        </div>
        {rows.map((row) => (
          <div key={row.key} role="row">
            <span role="cell">{row.series}</span>
            <span role="cell">{row.date}</span>
            <span role="cell">
              {formatNumber(row.value)}
              {unitLabel(unit)}
            </span>
          </div>
        ))}
      </div>
    </details>
  );
}

function SignedValue({ value }: { value: number | null }) {
  return (
    <span
      data-sign={value == null ? "none" : value > 0 ? "up" : value < 0 ? "down" : "flat"}
      role="cell"
    >
      {formatSigned(value)}
    </span>
  );
}

function EmptyChart({ title }: { title: string }) {
  return (
    <figure className="macro-chart">
      <figcaption>
        <strong>{title}</strong>
      </figcaption>
      <p className="macro-chart__empty">该图所需事实尚未回填。</p>
    </figure>
  );
}

function naturalWindow(points: MacroHistoryPoint[]): MacroHistoryPoint[] {
  if (points.length < 3) return points;
  const intervals = points
    .slice(1)
    .map((point, index) => {
      const current = Date.parse(point.date);
      const prior = Date.parse(points[index]?.date ?? "");
      return Number.isFinite(current) && Number.isFinite(prior)
        ? Math.max(1, Math.round((current - prior) / 86_400_000))
        : null;
    })
    .filter((value): value is number => value != null)
    .sort((left, right) => left - right);
  const median = intervals[Math.floor(intervals.length / 2)] ?? 1;
  const count = median <= 3 ? 260 : median <= 10 ? 156 : median <= 45 ? 120 : 40;
  return points.slice(-count);
}

function chartLineStyle(value: MacroChartSeries["lineStyle"]): LineStyle {
  if (value === "dashed") return LineStyle.Dashed;
  if (value === "dotted") return LineStyle.Dotted;
  return LineStyle.Solid;
}

function isTestDom(): boolean {
  return typeof window !== "undefined" && /jsdom/i.test(window.navigator.userAgent);
}

function formatOptional(value: number | null, unit?: string): string {
  return value == null ? "—" : `${formatNumber(value)}${unitLabel(unit)}`;
}

function formatNumber(value: number): string {
  return new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 }).format(value);
}

function formatSigned(value: number | null): string {
  if (value == null) return "—";
  return `${value > 0 ? "+" : ""}${formatNumber(value)}`;
}

function unitLabel(unit?: string): string {
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
      persons: " 人",
      thousands_persons: " 千人",
      usd_per_barrel: " 美元/桶",
      usdt: " USDT",
    }[unit] ?? ` ${unit}`
  );
}

function windowLabel(value: MacroCurveSnapshotView["window"]): string {
  return { current: "当前", "1w": "1周前", "1m": "1月前", "3m": "3月前" }[value];
}

function truncate(value: string, length: number): string {
  return value.length > length ? `${value.slice(0, length - 1)}…` : value;
}
