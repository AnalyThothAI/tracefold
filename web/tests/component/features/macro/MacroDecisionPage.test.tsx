import { MacroModulePage, MacroOverviewPage } from "@features/macro";
import { cleanup, screen, within } from "@testing-library/react";
import {
  macroModuleFixture,
  macroOverviewFixture,
  macroThesisFixture,
} from "@tests/fixtures/macroFixture";
import { server } from "@tests/msw/server";
import { renderWithProviders } from "@tests/render/renderWithProviders";
import { axe } from "jest-axe";
import { http, HttpResponse } from "msw";
import { afterEach, describe, expect, it } from "vitest";

const SESSION_PROPS = {
  bootstrapError: false,
  bootstrapLoading: false,
  token: "test-token",
};

describe("Macro Thin Thesis workbench", () => {
  afterEach(() => {
    cleanup();
    window.history.replaceState(null, "", window.location.pathname);
    window.location.hash = "";
  });

  it("renders twelve frozen asset facts while keeping outlooks sparse", async () => {
    server.use(
      http.get(/.*\/api\/macro\/overview$/, () =>
        HttpResponse.json({ ok: true, data: macroOverviewFixture() }),
      ),
    );
    const view = renderWithProviders(<MacroOverviewPage {...SESSION_PROPS} />, {
      route: "/macro",
    });

    expect(
      await screen.findByRole("heading", {
        name: "真实利率回落正在缓和风险资产的贴现压力",
      }),
    ).toBeVisible();
    expect(screen.getByRole("list", { name: "主线因果链" })).toBeVisible();
    expect(screen.getByText("尚未闭合的反证")).toBeVisible();
    expect(screen.getByText("本次真正重要的模块")).toBeVisible();
    expect(screen.getByText("十二资产事实固定呈现，展望只在有传导时出现")).toBeVisible();
    const assetTable = screen.getByRole("table", {
      name: "十二资产冻结事实与稀疏展望",
    });
    expect(within(assetTable).getAllByRole("row")).toHaveLength(13);
    expect(within(assetTable).getByText("SPY")).toBeVisible();
    expect(within(assetTable).getByText("QQQ")).toBeVisible();
    expect(screen.getByLabelText("QQQ 本次没有 material outlook")).toBeVisible();
    expect(screen.getByText("实际利率保持在五年分位数下尾，确认贴现压力缓和。")).toBeVisible();
    expect(screen.getAllByRole("link", { name: "利率与美联储" }).length).toBeGreaterThan(0);
    expect(await axe(view.container)).toHaveNoViolations();
  });

  it("keeps no_call sparse without inventing causal edges, material modules, or outlooks", async () => {
    const thesis = macroThesisFixture({
      mainline: {
        stance: "no_call",
        title: "证据冲突，暂不形成方向判断",
        thesis: "利率与信用证据尚未形成闭合传导。",
        stage: "uncertain",
        horizon: "1w",
        confidence: null,
        causal_edges: [],
        supporting_evidence_refs: [],
        conflicting_evidence_refs: ["credit:tail-gap"],
        no_call_reason: "缺少足以支撑方向判断的因果链。",
      },
      module_assessments: [],
      asset_outlooks: [],
      conditions: [],
      tensions: [],
      material_changes: [],
    });
    server.use(
      http.get(/.*\/api\/macro\/overview$/, () =>
        HttpResponse.json({
          ok: true,
          data: macroOverviewFixture({
            thesis,
            live_delta: null,
            outcome_replay: null,
            recovery: [],
          }),
        }),
      ),
    );

    renderWithProviders(<MacroOverviewPage {...SESSION_PROPS} />, { route: "/macro" });

    expect(
      await screen.findByRole("heading", { name: "证据冲突，暂不形成方向判断" }),
    ).toBeVisible();
    expect(screen.getByText("缺少足以支撑方向判断的因果链。")).toBeVisible();
    expect(screen.queryByRole("list", { name: "主线因果链" })).toBeNull();
    expect(
      screen.getByText("Agent 没有把任何模块提升为 material；这不是缺失的六宫格。"),
    ).toBeVisible();
    expect(screen.getByRole("table", { name: "十二资产冻结事实与稀疏展望" })).toBeVisible();
    expect(screen.getAllByLabelText(/本次没有 material outlook$/)).toHaveLength(12);
  });

  it("shows the exact current-session run state and never substitutes a prior publication", async () => {
    server.use(
      http.get(/.*\/api\/macro\/overview$/, () =>
        HttpResponse.json({
          ok: true,
          data: macroOverviewFixture({
            thesis_state: "running",
            thesis: null,
            thesis_reason: {
              code: "macro_thesis_running",
              message: "当前 session 的 Thin Agent 正在运行。",
              impact: "limited",
              affected_dataset_ids: [],
              affected_claim_ids: [],
              retryable: true,
              recovery: "automatic",
              next_action: null,
              next_check_at_ms: Date.now() + 60_000,
            },
            live_delta: null,
            outcome_replay: null,
            recovery: [],
          }),
        }),
      ),
    );

    renderWithProviders(<MacroOverviewPage {...SESSION_PROPS} />, { route: "/macro" });

    expect(await screen.findByRole("heading", { name: "生成中" })).toBeVisible();
    expect(screen.getByText("当前 session 的 Thin Agent 正在运行。")).toBeVisible();
    expect(screen.queryByText("真实利率回落正在缓和风险资产的贴现压力")).toBeNull();
  });

  it("labels same-session cached transport data as stale", async () => {
    server.use(
      http.get(/.*\/api\/macro\/overview$/, () =>
        HttpResponse.json({
          ok: true,
          data: macroOverviewFixture({
            transport: {
              state: "stale",
              last_successful_read_at_ms: Date.now() - 60_000,
              reason: {
                code: "macro_transport_stale",
                message: "当前 session 缓存陈旧。",
                impact: "limited",
                affected_dataset_ids: [],
                affected_claim_ids: [],
                retryable: true,
                recovery: "automatic",
                next_action: null,
                next_check_at_ms: Date.now() + 60_000,
              },
            },
          }),
        }),
      ),
    );

    renderWithProviders(<MacroOverviewPage {...SESSION_PROPS} />, { route: "/macro" });

    expect(await screen.findByText(/传输缓存可能陈旧/)).toBeVisible();
  });

  it("terminates bootstrap loading, bootstrap error, and missing-token states explicitly", () => {
    const loading = renderWithProviders(
      <MacroOverviewPage bootstrapError={false} bootstrapLoading token="" />,
      { route: "/macro" },
    );
    expect(screen.getByRole("status", { name: "建立读取会话" })).toBeVisible();
    loading.unmount();

    const failed = renderWithProviders(
      <MacroOverviewPage bootstrapError bootstrapLoading={false} token="" />,
      { route: "/macro" },
    );
    expect(screen.getByText("宏观读取会话建立失败。")).toBeVisible();
    failed.unmount();

    renderWithProviders(
      <MacroOverviewPage bootstrapError={false} bootstrapLoading={false} token="" />,
      { route: "/macro" },
    );
    expect(screen.getByText("宏观读取会话不可用")).toBeVisible();
    expect(screen.queryByRole("status", { name: "建立读取会话" })).toBeNull();
  });

  it("shows v2 module context plus independent current, history, and contract planes", async () => {
    server.use(
      http.get(/.*\/api\/macro\/rates-fed$/, () =>
        HttpResponse.json({ ok: true, data: macroModuleFixture("rates_fed") }),
      ),
    );

    renderWithProviders(<MacroModulePage {...SESSION_PROPS} moduleId="rates_fed" />, {
      route: "/macro/rates-fed",
    });

    expect(await screen.findByRole("heading", { level: 1, name: "利率与美联储" })).toBeVisible();
    expect(screen.getByText(/当前事实 当前/)).toBeVisible();
    expect(screen.getByText(/required 历史 完整/)).toBeVisible();
    expect(screen.getByText(/数据合同 完整/)).toBeVisible();
    expect(
      screen.getByRole("heading", {
        name: "最近完整交易日：2Y 下行4bp，10Y 上行6bp，30Y 上行11bp（2026-07-29）",
      }),
    ).toBeVisible();
    expect(screen.getByRole("table", { name: "2Y 10Y 30Y 收益率矩阵" })).toBeVisible();
    expect(screen.getByText("Thesis · 驱动主线")).toBeVisible();
    expect(screen.queryByRole("heading", { name: "实际利率是本次主线的主要驱动。" })).toBeNull();
  });

  it("owns the official CFE curve on volatility and does not duplicate it on cross-asset", async () => {
    window.location.hash = "#term";
    server.use(
      http.get(/.*\/api\/macro\/volatility$/, () =>
        HttpResponse.json({ ok: true, data: macroModuleFixture("volatility") }),
      ),
    );
    const volatility = renderWithProviders(
      <MacroModulePage {...SESSION_PROPS} moduleId="volatility" />,
      { route: "/macro/volatility#term" },
    );
    expect(await screen.findByText("VXQ26")).toBeVisible();
    expect(screen.getByText("2026-08-19")).toBeVisible();
    volatility.unmount();

    window.location.hash = "#futures";
    server.use(
      http.get(/.*\/api\/macro\/cross-asset$/, () =>
        HttpResponse.json({ ok: true, data: macroModuleFixture("cross_asset") }),
      ),
    );
    renderWithProviders(<MacroModulePage {...SESSION_PROPS} moduleId="cross_asset" />, {
      route: "/macro/cross-asset#futures",
    });
    expect(await screen.findByRole("heading", { name: "期货市场与仓位确认" })).toBeVisible();
    expect(screen.queryByText("VXQ26")).toBeNull();
  });
});
