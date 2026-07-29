import {
  ColorType,
  CrosshairMode,
  LineSeries,
  LineStyle,
  createChart,
  createSeriesMarkers,
  type ISeriesApi,
  type LineData,
  type SeriesMarker,
  type Time,
} from "lightweight-charts";
import { useEffect, useId, useMemo, useRef, useState } from "react";
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

export type MacroChartAnnotation = {
  id: string;
  date: string;
  label: string;
  detail?: string;
  seriesId?: string;
  sourceTimestamp?: string;
  value?: number;
  tone?:
    | "latest"
    | "cutoff"
    | "change"
    | "confirming"
    | "weakening"
    | "invalidation"
    | "checkpoint";
  showPriceLine?: boolean;
};

type ResolvedChartAnnotation = Required<
  Pick<MacroChartAnnotation, "id" | "date" | "label" | "seriesId" | "value" | "tone">
> &
  Pick<MacroChartAnnotation, "detail" | "showPriceLine"> & {
    positionAtValue: boolean;
    projection: "exact" | "nearest" | "clamped-start" | "clamped-end";
    seriesLabel: string;
    sourceDate: string;
    sourceTimestamp: string;
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

const EMPTY_ANNOTATIONS: MacroChartAnnotation[] = [];

export function MacroTimeSeriesChart({
  title,
  description,
  series,
  unit,
  baseline,
  defaultRange = "natural",
  annotations = EMPTY_ANNOTATIONS,
}: {
  title: string;
  description?: string;
  series: MacroChartSeries[];
  unit?: string;
  baseline?: number;
  defaultRange?: "natural" | "all";
  annotations?: MacroChartAnnotation[];
}) {
  const headingId = useId();
  const descriptionId = useId();
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
  const resolvedAnnotations = useMemo(
    () => resolveChartAnnotations(visibleSeries, annotations),
    [annotations, visibleSeries],
  );
  const hasPoints = visibleSeries.some((item) => item.points.length);
  const takeaway = description ?? timeSeriesTakeaway(visibleSeries, unit);

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
      id: string;
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
      chartSeries.push({ api, id: item.id, label: item.label });
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
    chartSeries.forEach(({ api, id }) => {
      const bound = resolvedAnnotations
        .filter((annotation) => annotation.seriesId === id)
        .sort((left, right) => left.date.localeCompare(right.date));
      if (bound.length) {
        createSeriesMarkers(
          api,
          bound.map((annotation): SeriesMarker<Time> => {
            const marker = {
              color: annotationColor(annotation.tone),
              id: annotation.id,
              shape: annotationShape(annotation.tone),
              text: annotation.tone === "latest" ? undefined : annotation.label,
              time: annotation.date as Time,
            };
            return annotation.positionAtValue
              ? {
                  ...marker,
                  position: "atPriceMiddle",
                  price: annotation.value,
                }
              : {
                  ...marker,
                  position: annotation.tone === "invalidation" ? "aboveBar" : "belowBar",
                };
          }),
        );
      }
      bound
        .filter((annotation) => annotation.showPriceLine)
        .forEach((annotation) => {
          api.createPriceLine({
            axisLabelVisible: true,
            color: annotationColor(annotation.tone),
            lineStyle: LineStyle.Dashed,
            lineWidth: 1,
            price: annotation.value,
            title: annotation.label,
          });
        });
    });
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
  }, [baseline, hasPoints, resolvedAnnotations, unit, visibleSeries]);

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
    <figure
      aria-describedby={descriptionId}
      aria-labelledby={headingId}
      className="macro-chart"
      data-chart-kind="time-series"
    >
      <figcaption>
        <div>
          <h3 id={headingId}>{title}</h3>
          <p id={descriptionId}>{takeaway}</p>
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
      <ChartAnnotationList annotations={resolvedAnnotations} title={title} unit={unit} />
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
      <PaginatedSeriesTable series={visibleSeries} title={title} unit={unit} />
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
  const headingId = useId();
  const descriptionId = useId();
  const plottedSnapshots = snapshots.map((snapshot) => ({
    ...snapshot,
    points: [...snapshot.points].sort((left, right) => left.years - right.years),
  }));
  const values = plottedSnapshots.flatMap((snapshot) =>
    snapshot.points.map((point) => point.value),
  );
  if (!values.length) {
    return <EmptyChart title={title} />;
  }
  const minimum = Math.min(...values);
  const maximum = Math.max(...values);
  const span = maximum - minimum || 1;
  const maturities = plottedSnapshots.flatMap((snapshot) =>
    snapshot.points.map((point) => point.years),
  );
  const minimumYears = Math.min(...maturities);
  const maximumYears = Math.max(...maturities);
  const maturitySpan = maximumYears - minimumYears;
  const axisSnapshot =
    plottedSnapshots.find((snapshot) => snapshot.window === "current") ??
    plottedSnapshots.reduce(
      (longest, snapshot) => (snapshot.points.length > longest.points.length ? snapshot : longest),
      snapshots[0]!,
    );
  return (
    <figure
      aria-describedby={descriptionId}
      aria-labelledby={headingId}
      className="macro-chart macro-chart--curve"
      data-chart-kind="curve"
    >
      <figcaption>
        <div>
          <h3 id={headingId}>{title}</h3>
          <p id={descriptionId}>
            按实际到期年数定位横轴；当前、1周、1月和3月快照共享同一 maturity scale。
          </p>
        </div>
      </figcaption>
      <svg aria-label={title} role="img" viewBox="0 0 720 300">
        <title>{title}</title>
        <desc>收益率曲线横轴按实际到期年数缩放，折线比较四个历史快照。</desc>
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
        {plottedSnapshots.map((snapshot, index) => (
          <path
            className="macro-chart__curve-line"
            d={snapshot.points
              .map((point, pointIndex) => {
                const x = curvePointX(point.years, minimumYears, maturitySpan);
                const y = 256 - ((point.value - minimum) / span) * 232;
                return `${pointIndex ? "L" : "M"} ${x.toFixed(2)} ${y.toFixed(2)}`;
              })
              .join(" ")}
            data-window={snapshot.window}
            key={snapshot.window}
            style={{ "--series-color": SERIES_COLORS[index] } as CSSProperties}
          />
        ))}
        {plottedSnapshots
          .find((snapshot) => snapshot.window === "current")
          ?.points.map((point) => {
            const x = curvePointX(point.years, minimumYears, maturitySpan);
            const y = 256 - ((point.value - minimum) / span) * 232;
            return (
              <circle
                className="macro-chart__curve-point"
                cx={x}
                cy={y}
                key={`current:${point.tenor}`}
                r="3.5"
              />
            );
          })}
        {axisSnapshot.points.map((point) => {
          const x = curvePointX(point.years, minimumYears, maturitySpan);
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
        {plottedSnapshots.map((snapshot, index) => (
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
        <table className="macro-chart__table">
          <caption>{title}等价数据表</caption>
          <thead>
            <tr>
              <th scope="col">窗口</th>
              <th scope="col">截至</th>
              <th scope="col">期限与数值</th>
            </tr>
          </thead>
          <tbody>
            {plottedSnapshots.map((snapshot) => (
              <tr key={snapshot.window}>
                <td>{windowLabel(snapshot.window)}</td>
                <td>{snapshot.asOf}</td>
                <td>
                  {snapshot.points
                    .map(
                      (point) =>
                        `${point.tenor} (${formatNumber(point.years)}Y) ${formatNumber(point.value)}${unit}`,
                    )
                    .join(" · ")}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
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
  const headingId = useId();
  const descriptionId = useId();
  const numericValues = groups.flatMap((group) =>
    group.values.flatMap((item) => (item.value == null ? [] : [item.value])),
  );
  if (!numericValues.length) return <EmptyChart title={title} />;
  const minimum = Math.min(baseline, ...numericValues);
  const maximum = Math.max(baseline, ...numericValues);
  const span = maximum - minimum || 1;
  const baselineY = 244 - ((baseline - minimum) / span) * 204;
  const groupWidth = 620 / Math.max(groups.length, 1);
  const takeaway =
    description ?? `${groups.length} 个分组共享同一数值轴；零值保留为真实观察，不按缺失处理。`;
  return (
    <figure
      aria-describedby={descriptionId}
      aria-labelledby={headingId}
      className="macro-chart macro-chart--bars"
      data-chart-kind="bar"
    >
      <figcaption>
        <div>
          <h3 id={headingId}>{title}</h3>
          <p id={descriptionId}>{takeaway}</p>
        </div>
      </figcaption>
      <svg aria-label={title} role="img" viewBox="0 0 720 290">
        <title>{title}</title>
        <desc>{takeaway}</desc>
        {[0, 1, 2, 3, 4].map((index) => {
          const y = 40 + index * 51;
          const value = maximum - (span * index) / 4;
          return (
            <g key={`grid:${index}`}>
              <line className="macro-chart__grid-line" x1="76" x2="696" y1={y} y2={y} />
              <text className="macro-chart__axis-label" x="6" y={y + 4}>
                {formatNumber(value)}
              </text>
            </g>
          );
        })}
        <line className="macro-chart__baseline" x1="76" x2="696" y1={baselineY} y2={baselineY} />
        {groups.map((group, groupIndex) => {
          const barWidth = Math.min(28, (groupWidth - 18) / Math.max(group.values.length, 1));
          const drawableWidth = barWidth * group.values.length;
          const groupLeft = 76 + groupIndex * groupWidth;
          const barStart = groupLeft + Math.max((groupWidth - drawableWidth) / 2, 4);
          return (
            <g key={group.id}>
              {group.values.map((item, itemIndex) => {
                if (item.value == null) return null;
                const x = barStart + itemIndex * barWidth;
                const valueY = 244 - ((item.value - minimum) / span) * 204;
                const y = Math.min(valueY, baselineY);
                const height = Math.max(Math.abs(valueY - baselineY), 1);
                return (
                  <g key={item.id}>
                    <rect
                      className="macro-chart__bar"
                      height={height}
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
                    <text
                      className="macro-chart__bar-value"
                      textAnchor="middle"
                      x={x + Math.max(barWidth - 4, 5) / 2}
                      y={
                        item.value >= baseline
                          ? Math.max(valueY - 6, 16)
                          : Math.min(valueY + 14, 258)
                      }
                    >
                      {formatNumber(item.value)}
                    </text>
                  </g>
                );
              })}
              <text
                className="macro-chart__axis-label"
                textAnchor="middle"
                x={groupLeft + groupWidth / 2}
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
        <table className="macro-chart__table">
          <caption>{title}等价数据表</caption>
          <thead>
            <tr>
              <th scope="col">分组</th>
              <th scope="col">系列</th>
              <th scope="col">数值</th>
            </tr>
          </thead>
          <tbody>
            {groups.flatMap((group) =>
              group.values.map((item) => (
                <tr key={`${group.id}:${item.id}`}>
                  <td>{group.label}</td>
                  <td>{item.label}</td>
                  <td>
                    {item.value == null ? "—" : `${formatNumber(item.value)}${unitLabel(unit)}`}
                  </td>
                </tr>
              )),
            )}
          </tbody>
        </table>
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
  const headingId = useId();
  const descriptionId = useId();
  if (!rows.length) return <EmptyChart title={title} />;
  const symbols = [...new Set(rows.flatMap((row) => [row.left, row.right]))];
  const lookup = new Map<string, MacroCorrelationView>();
  rows.forEach((row) => {
    lookup.set(`${row.left}:${row.right}`, row);
    lookup.set(`${row.right}:${row.left}`, row);
  });
  return (
    <figure
      aria-describedby={descriptionId}
      aria-labelledby={headingId}
      className="macro-chart macro-chart--heatmap"
      data-chart-kind="heatmap"
    >
      <figcaption>
        <div>
          <h3 id={headingId}>{title}</h3>
          <p id={descriptionId}>颜色表达方向与强度；每一对资产保留共同样本数和观察窗口。</p>
        </div>
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
      <details className="macro-chart__data">
        <summary>查看相关性数据（{rows.length} 对）</summary>
        <table className="macro-chart__table">
          <caption>{title}等价数据表</caption>
          <thead>
            <tr>
              <th scope="col">资产对</th>
              <th scope="col">相关系数</th>
              <th scope="col">样本 / 窗口</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={`${row.left}:${row.right}`}>
                <td>
                  {row.left} / {row.right}
                </td>
                <td>{row.correlation == null ? "—" : formatNumber(row.correlation)}</td>
                <td>
                  n={row.sampleCount ?? "—"} · {row.window ?? "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </details>
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
      <table className="macro-chart__table macro-chart__table--indicators">
        <caption>{title}等价数据表</caption>
        <thead>
          <tr>
            <th scope="col">指标</th>
            <th scope="col">最新</th>
            <th scope="col">1周</th>
            <th scope="col">1月</th>
            <th scope="col">样本/分位</th>
            <th scope="col">截至</th>
          </tr>
        </thead>
        <tbody>
          {indicators.map((item) => (
            <tr key={item.datasetId}>
              <td>
                <strong>{item.label}</strong>
                <small>{item.datasetId}</small>
              </td>
              <td>{formatOptional(item.latestValue, item.unit)}</td>
              <SignedValue value={item.change1w} />
              <SignedValue value={item.change1m} />
              <td>
                {item.sampleCount ?? "—"} /{" "}
                {item.percentile == null ? "—" : `${formatNumber(item.percentile)}%`}
              </td>
              <td>
                {item.asOf ?? "—"}
                {item.sourceUrl ? (
                  <a href={item.sourceUrl} rel="noreferrer" target="_blank">
                    来源
                  </a>
                ) : null}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </details>
  );
}

export function MacroReleaseStrip({ releases }: { releases: MacroReleaseView[] }) {
  if (!releases.length) return null;
  return (
    <section aria-label="官方发布事实" className="macro-chart__release-strip">
      {releases.map((release) => (
        <article
          key={`${release.datasetId}:${release.referencePeriod ?? release.publishedAtMs ?? release.scheduledAtMs ?? "latest"}`}
        >
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
              ? `相对预期 ${formatSigned(release.surprise)} · `
              : null}
            前值修订 {formatSigned(release.revision)}
          </small>
          {release.publishedAtMs || release.receivedAtMs ? (
            <small>
              {release.publishedAtMs
                ? `发布 ${formatInstant(release.publishedAtMs)}`
                : "未提供发布时间"}
              {release.receivedAtMs ? ` · 接收 ${formatInstant(release.receivedAtMs)}` : ""}
            </small>
          ) : null}
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

function PaginatedSeriesTable({
  series,
  title,
  unit,
}: {
  series: MacroChartSeries[];
  title: string;
  unit?: string;
}) {
  const [visibleCount, setVisibleCount] = useState(24);
  const rows = useMemo(
    () =>
      series
        .flatMap((item) =>
          item.points.map((point) => ({
            key: `${item.id}:${point.date}`,
            series: item.label,
            date: point.date,
            value: point.value,
          })),
        )
        .sort((left, right) => right.date.localeCompare(left.date)),
    [series],
  );
  useEffect(() => setVisibleCount(24), [series]);
  if (!rows.length) return null;
  return (
    <details className="macro-chart__data">
      <summary>查看图表数据（{rows.length} 个观测）</summary>
      <table className="macro-chart__table">
        <caption>{title}等价数据表</caption>
        <thead>
          <tr>
            <th scope="col">系列</th>
            <th scope="col">日期</th>
            <th scope="col">数值</th>
          </tr>
        </thead>
        <tbody>
          {rows.slice(0, visibleCount).map((row) => (
            <tr key={row.key}>
              <td>{row.series}</td>
              <td>{row.date}</td>
              <td>
                {formatNumber(row.value)}
                {unitLabel(unit)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {visibleCount < rows.length ? (
        <button
          className="macro-chart__more"
          onClick={() => setVisibleCount((count) => Math.min(count + 48, rows.length))}
          type="button"
        >
          显示更多（剩余 {rows.length - visibleCount}）
        </button>
      ) : null}
    </details>
  );
}

function ChartAnnotationList({
  annotations,
  title,
  unit,
}: {
  annotations: ResolvedChartAnnotation[];
  title: string;
  unit?: string;
}) {
  const decisionAnnotations = annotations.filter((annotation) => annotation.tone !== "latest");
  if (!decisionAnnotations.length) return null;
  return (
    <ul aria-label={`${title}坐标注释`} className="macro-chart__annotations">
      {decisionAnnotations.map((annotation) => (
        <li
          data-annotation-id={annotation.id}
          data-coordinate-date={annotation.date}
          data-coordinate-value={annotation.value}
          data-projection={annotation.projection}
          data-series-id={annotation.seriesId}
          data-source-date={annotation.sourceDate}
          data-source-timestamp={annotation.sourceTimestamp}
          data-tone={annotation.tone}
          key={`${annotation.seriesId}:${annotation.id}`}
        >
          <time dateTime={annotation.sourceTimestamp}>{annotation.sourceDate}</time>
          <strong>{annotation.label}</strong>
          <span>
            {annotation.seriesLabel} · {formatNumber(annotation.value)}
            {unitLabel(unit)}
          </span>
          {annotation.projection === "exact" ? null : (
            <small>{annotationProjectionLabel(annotation)}</small>
          )}
          {annotation.detail ? <small>{annotation.detail}</small> : null}
        </li>
      ))}
    </ul>
  );
}

function SignedValue({ value }: { value: number | null }) {
  return (
    <td data-sign={value == null ? "none" : value > 0 ? "up" : value < 0 ? "down" : "flat"}>
      {formatSigned(value)}
    </td>
  );
}

function EmptyChart({ title }: { title: string }) {
  const headingId = useId();
  const descriptionId = useId();
  return (
    <figure
      aria-describedby={descriptionId}
      aria-labelledby={headingId}
      className="macro-chart"
      data-chart-kind="empty"
    >
      <figcaption>
        <div>
          <h3 id={headingId}>{title}</h3>
          <p id={descriptionId}>当前没有可绘制的结构化事实；不生成静态占位图。</p>
        </div>
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
  const latestMs = Date.parse(points.at(-1)?.date ?? "");
  if (!Number.isFinite(latestMs)) {
    const fallbackCount = median <= 3 ? 260 : median <= 10 ? 156 : median <= 45 ? 120 : 40;
    return points.slice(-fallbackCount);
  }
  const windowDays =
    median <= 3 ? 366 : median <= 10 ? 3 * 366 : median <= 45 ? 10 * 366 : 40 * median;
  const cutoffMs = latestMs - windowDays * 86_400_000;
  const selected = points.filter((point) => Date.parse(point.date) >= cutoffMs);
  return selected.length >= 2 ? selected : points.slice(-2);
}

function resolveChartAnnotations(
  series: MacroChartSeries[],
  annotations: MacroChartAnnotation[],
): ResolvedChartAnnotation[] {
  const resolved: ResolvedChartAnnotation[] = [];
  series.forEach((item) => {
    const latest = item.points.at(-1);
    if (!latest) return;
    resolved.push({
      id: `latest:${item.id}`,
      date: latest.date,
      label: "最新观测",
      seriesId: item.id,
      seriesLabel: item.label,
      tone: "latest",
      value: latest.value,
      positionAtValue: false,
      projection: "exact",
      sourceDate: latest.date,
      sourceTimestamp: latest.date,
    });
  });
  annotations.forEach((annotation) => {
    const target = annotation.seriesId
      ? series.find((item) => item.id === annotation.seriesId)
      : series[0];
    if (!target?.points.length) return;
    const projected = projectVisiblePoint(target.points, annotation.date);
    if (!projected) return;
    resolved.push({
      ...annotation,
      date: projected.point.date,
      seriesId: target.id,
      seriesLabel: target.label,
      tone: annotation.tone ?? "change",
      value: Number.isFinite(annotation.value) ? annotation.value! : projected.point.value,
      positionAtValue: Number.isFinite(annotation.value),
      projection: projected.projection,
      sourceDate: annotation.date,
      sourceTimestamp: annotation.sourceTimestamp ?? annotation.date,
    });
  });
  return resolved;
}

function projectVisiblePoint(
  points: MacroHistoryPoint[],
  targetDate: string,
): {
  point: MacroHistoryPoint;
  projection: ResolvedChartAnnotation["projection"];
} | null {
  const targetMs = Date.parse(targetDate);
  if (!Number.isFinite(targetMs)) {
    const exact = points.find((point) => point.date === targetDate);
    return exact ? { point: exact, projection: "exact" } : null;
  }
  const dated = points
    .map((point) => ({ point, time: Date.parse(point.date) }))
    .filter((item) => Number.isFinite(item.time))
    .sort((left, right) => left.time - right.time);
  if (!dated.length) return null;
  const first = dated[0]!;
  const last = dated.at(-1)!;
  if (targetMs < first.time) {
    return { point: first.point, projection: "clamped-start" };
  }
  if (targetMs > last.time) {
    return { point: last.point, projection: "clamped-end" };
  }
  const nearest = dated.reduce((candidateNearest, candidate) =>
    Math.abs(candidate.time - targetMs) < Math.abs(candidateNearest.time - targetMs)
      ? candidate
      : candidateNearest,
  );
  return {
    point: nearest.point,
    projection: nearest.time === targetMs ? "exact" : "nearest",
  };
}

function annotationProjectionLabel(annotation: ResolvedChartAnnotation): string {
  const source = formatAnnotationTimestamp(annotation.sourceTimestamp);
  if (annotation.projection === "clamped-start") {
    return `实际锚点 ${source}；图上置于起点观测 ${annotation.date}。`;
  }
  if (annotation.projection === "clamped-end") {
    return `实际锚点 ${source}；图上置于末端观测 ${annotation.date}。`;
  }
  return `实际锚点 ${source}；图上置于最近观测 ${annotation.date}。`;
}

function formatAnnotationTimestamp(value: string): string {
  if (/^\d{4}-\d{2}-\d{2}$/u.test(value)) return value;
  const timestamp = Date.parse(value);
  if (!Number.isFinite(timestamp)) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    dateStyle: "medium",
    hour12: false,
    timeStyle: "short",
  }).format(new Date(timestamp));
}

function timeSeriesTakeaway(series: MacroChartSeries[], unit?: string): string {
  const latest = series
    .flatMap((item) => {
      const point = item.points.at(-1);
      return point
        ? [`${item.label} ${formatNumber(point.value)}${unitLabel(unit)}（${point.date}）`]
        : [];
    })
    .slice(0, 3);
  return latest.length
    ? `最新：${latest.join(" · ")}；自然窗口按各序列发布频率截取。`
    : "当前窗口没有可绘制观测。";
}

function curvePointX(years: number, minimumYears: number, maturitySpan: number): number {
  if (!maturitySpan) return 379;
  return 58 + ((years - minimumYears) / maturitySpan) * 642;
}

function annotationColor(tone: ResolvedChartAnnotation["tone"]): string {
  if (tone === "invalidation") return "#ff7c8b";
  if (tone === "weakening" || tone === "checkpoint") return "#f6c85f";
  if (tone === "confirming") return "#66c2a5";
  if (tone === "cutoff") return "#9d8cff";
  return "#67d4ff";
}

function annotationShape(tone: ResolvedChartAnnotation["tone"]): SeriesMarker<Time>["shape"] {
  if (tone === "invalidation") return "arrowDown";
  if (tone === "confirming") return "arrowUp";
  if (tone === "checkpoint" || tone === "cutoff") return "square";
  return "circle";
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

function formatInstant(value: number): string {
  return new Intl.DateTimeFormat("zh-CN", {
    dateStyle: "short",
    hour12: false,
    timeStyle: "short",
  }).format(new Date(value));
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
      billions_chained_2017_usd: " 十亿 2017 年不变价美元",
      billions_usd: " 十亿美元",
      bp: " bp",
      index: " 点",
      index_points: " 点",
      millions_usd: " 百万美元",
      percent: "%",
      percent_open_interest: "% OI",
      persons: " 人",
      thousands_persons: " 千人",
      usd_per_barrel: " 美元/桶",
      usdt: " USDT",
    }[unit] ?? "（单位未解释）"
  );
}

function windowLabel(value: MacroCurveSnapshotView["window"]): string {
  return { current: "当前", "1w": "1周前", "1m": "1月前", "3m": "3月前" }[value];
}

function truncate(value: string, length: number): string {
  return value.length > length ? `${value.slice(0, length - 1)}…` : value;
}
