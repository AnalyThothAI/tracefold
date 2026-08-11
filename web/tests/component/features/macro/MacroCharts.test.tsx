import {
  MacroBarChart,
  MacroCorrelationHeatmap,
  MacroCurveChart,
  MacroTimeSeriesChart,
} from "@features/macro/ui/MacroCharts";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

describe("macro chart semantics", () => {
  afterEach(cleanup);

  it("positions Treasury tenors by maturity years instead of array index", () => {
    const { container } = render(
      <MacroCurveChart
        snapshots={[
          {
            asOf: "2026-07-28",
            points: [
              { tenor: "3M", value: 4.4, years: 0.25 },
              { tenor: "2Y", value: 4.1, years: 2 },
              { tenor: "10Y", value: 4.3, years: 10 },
            ],
            window: "current",
          },
        ]}
        title="名义 Treasury 曲线"
      />,
    );

    const path = container.querySelector('path[data-window="current"]');
    expect(path).not.toBeNull();
    const coordinates = [...(path?.getAttribute("d") ?? "").matchAll(/[ML] ([\d.]+) ([\d.]+)/g)];
    const x = coordinates.map((match) => Number(match[1]));

    expect(x).toHaveLength(3);
    expect(x[1]! - x[0]!).toBeLessThan(x[2]! - x[1]!);
    expect(x[2]! - x[1]!).toBeGreaterThan(400);
    expect(screen.getByRole("img", { name: "名义 Treasury 曲线" })).toHaveAttribute(
      "viewBox",
      "0 0 720 300",
    );
    expect(screen.getByRole("heading", { name: "名义 Treasury 曲线" })).toBeVisible();
    expect(screen.getByText(/按实际到期年数定位横轴/)).toBeVisible();
    expect(screen.getByRole("figure", { name: "名义 Treasury 曲线" })).toHaveAccessibleDescription(
      /按实际到期年数定位横轴/,
    );
    fireEvent.click(screen.getByText("查看期限数据"));
    expect(screen.getByRole("table", { name: "名义 Treasury 曲线等价数据表" })).toBeVisible();
  });

  it("exposes point-bound annotations, a takeaway, and keyboard-reachable equivalent data", () => {
    const { container } = render(
      <MacroTimeSeriesChart
        annotations={[
          {
            date: "2026-07-01",
            id: "thesis:publication-fixture:cutoff",
            label: "Thesis 截点",
            seriesId: "real-yield",
            tone: "cutoff",
          },
          {
            date: "2026-07-02",
            detail: "实际利率继续抬升。",
            id: "change:real-yield",
            label: "关键变化",
            seriesId: "real-yield",
            tone: "change",
          },
          {
            date: "2026-07-02",
            id: "claim:claim-real-yield:confirmation",
            label: "确认条件",
            seriesId: "real-yield",
            tone: "confirming",
            value: 2,
          },
          {
            date: "2026-07-03",
            id: "falsifier:condition-real-yield",
            label: "失效阈值",
            seriesId: "real-yield",
            showPriceLine: true,
            tone: "invalidation",
            value: 2.1,
          },
          {
            date: "2026-07-03",
            id: "checkpoint:condition-real-yield",
            label: "检查点",
            seriesId: "real-yield",
            tone: "checkpoint",
            value: 2.05,
          },
        ]}
        series={[
          {
            id: "real-yield",
            label: "10Y 实际利率",
            points: [
              { date: "2026-07-01", value: 1.91 },
              { date: "2026-07-02", value: 1.98 },
              { date: "2026-07-03", value: 2.04 },
            ],
          },
        ]}
        title="实际利率"
        unit="percent"
      />,
    );

    expect(screen.getByRole("heading", { name: "实际利率" })).toBeVisible();
    expect(screen.getByText(/最新：10Y 实际利率 2.04%/)).toBeVisible();
    expect(screen.getByRole("figure", { name: "实际利率" })).toHaveAccessibleDescription(
      /最新：10Y 实际利率 2.04%/,
    );
    expect(screen.getByLabelText("实际利率坐标注释")).toHaveTextContent(
      "2026-07-02关键变化10Y 实际利率 · 1.98%",
    );
    expect(screen.getByText("实际利率继续抬升。")).toBeVisible();
    const chartButton = screen.getByRole("button", { name: "实际利率，点击锁定读数" });
    expect(chartButton).toHaveAttribute("tabindex", "0");
    fireEvent.keyDown(chartButton, { key: "Enter" });
    expect(screen.getByText(/已锁定/)).toBeVisible();
    fireEvent.keyDown(chartButton, { key: " " });
    expect(screen.queryByText(/已锁定/)).toBeNull();
    expect(
      container.querySelector('[data-annotation-id="thesis:publication-fixture:cutoff"]'),
    ).toHaveAttribute("data-coordinate-date", "2026-07-01");
    expect(
      container.querySelector('[data-annotation-id="claim:claim-real-yield:confirmation"]'),
    ).toHaveAttribute("data-coordinate-value", "2");
    expect(
      container.querySelector('[data-annotation-id="falsifier:condition-real-yield"]'),
    ).toHaveAttribute("data-tone", "invalidation");
    expect(
      container.querySelector('[data-annotation-id="checkpoint:condition-real-yield"]'),
    ).toHaveAttribute("data-series-id", "real-yield");
    fireEvent.click(screen.getByText("查看图表数据（3 个观测）"));
    expect(screen.getByRole("table", { name: "实际利率等价数据表" })).toBeVisible();
  });

  it("clamps out-of-range annotations to real plot boundaries without losing source time", () => {
    const { container } = render(
      <MacroTimeSeriesChart
        annotations={[
          {
            date: "2026-06-29",
            id: "condition:before-range",
            label: "起点前条件",
            seriesId: "real-yield",
            sourceTimestamp: "2026-06-29T08:50:00.000Z",
            tone: "checkpoint",
          },
          {
            date: "2026-07-05",
            id: "condition:after-range",
            label: "末端后条件",
            seriesId: "real-yield",
            sourceTimestamp: "2026-07-05T08:50:00.000Z",
            tone: "invalidation",
            value: 2.1,
          },
        ]}
        series={[
          {
            id: "real-yield",
            label: "10Y 实际利率",
            points: [
              { date: "2026-07-01", value: 1.91 },
              { date: "2026-07-02", value: 1.98 },
              { date: "2026-07-03", value: 2.04 },
            ],
          },
        ]}
        title="实际利率"
        unit="percent"
      />,
    );

    const before = container.querySelector('[data-annotation-id="condition:before-range"]');
    expect(before).toHaveAttribute("data-coordinate-date", "2026-07-01");
    expect(before).toHaveAttribute("data-source-timestamp", "2026-06-29T08:50:00.000Z");
    expect(before).toHaveAttribute("data-projection", "clamped-start");
    const after = container.querySelector('[data-annotation-id="condition:after-range"]');
    expect(after).toHaveAttribute("data-coordinate-date", "2026-07-03");
    expect(after).toHaveAttribute("data-source-timestamp", "2026-07-05T08:50:00.000Z");
    expect(after).toHaveAttribute("data-projection", "clamped-end");
    expect(screen.getByText(/图上置于起点观测 2026-07-01/)).toBeVisible();
    expect(screen.getByText(/图上置于末端观测 2026-07-03/)).toBeVisible();
  });

  it("keeps a true zero as a plotted bar and an equivalent table value", () => {
    const { container } = render(
      <MacroBarChart
        groups={[
          {
            id: "dealer",
            label: "Dealer",
            values: [{ id: "net", label: "净仓位", value: 0 }],
          },
        ]}
        title="净仓位"
      />,
    );

    expect(container.querySelector(".macro-chart__bar")).toHaveAttribute("height", "1");
    expect(screen.getByRole("heading", { name: "净仓位" })).toBeVisible();
    expect(screen.getByRole("figure", { name: "净仓位" })).toHaveAccessibleDescription(
      /零值保留为真实观察/,
    );
    fireEvent.click(screen.getByText("查看柱图数据"));
    expect(screen.getByRole("table", { name: "净仓位等价数据表" })).toHaveTextContent(
      "Dealer净仓位0",
    );
  });

  it("gives the correlation matrix a takeaway and complete pair table", () => {
    render(
      <MacroCorrelationHeatmap
        rows={[
          {
            correlation: 0.62,
            left: "SPY",
            right: "TLT",
            sampleCount: 118,
            window: "90_daily_returns",
          },
          {
            correlation: -0.31,
            left: "SPY",
            right: "DXY",
            sampleCount: 116,
            window: "90_daily_returns",
          },
        ]}
        title="日收益相关矩阵"
      />,
    );

    expect(screen.getByRole("figure", { name: "日收益相关矩阵" })).toHaveAccessibleDescription(
      /颜色表达方向与强度/,
    );
    fireEvent.click(screen.getByText("查看相关性数据（2 对）"));
    expect(screen.getByRole("table", { name: "日收益相关矩阵等价数据表" })).toHaveTextContent(
      "SPY / TLT0.62n=118 · 90_daily_returns",
    );
  });
});
