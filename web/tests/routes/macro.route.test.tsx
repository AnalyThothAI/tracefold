import type { MacroResearchReadData } from "@features/macro";
import { screen, waitFor, within } from "@testing-library/react";
import {
  macroModuleFixture,
  macroOverviewFixture,
  macroResearchFixture,
  macroThesisFixture,
} from "@tests/fixtures/macroFixture";
import { ok } from "@tests/msw/fixtures";
import { mockLiveRadarRoute } from "@tests/msw/scenarios";
import { renderAppRoute } from "@tests/render/renderRoute";
import { axe } from "jest-axe";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { apiMock, setupAppRouteTest } from "./routeTestSetup";

describe("daily macro Thin Thesis routes", () => {
  afterEach(() => {
    window.location.hash = "";
    document.body.replaceChildren();
  });

  beforeEach(() => {
    configureMacroApi(macroResearchFixture());
  });

  it("renders the one-session sparse overview", async () => {
    const view = renderAppRoute("/macro");

    expect(await screen.findByRole("heading", { level: 1, name: "每日宏观主线" })).toBeVisible();
    expect(
      screen.getByRole("heading", {
        name: "真实利率回落正在缓和风险资产的贴现压力",
      }),
    ).toBeVisible();
    expect(screen.getByText("十二资产事实固定呈现，展望只在有传导时出现")).toBeVisible();
    const assetTable = screen.getByRole("table", {
      name: "十二资产冻结事实与稀疏展望",
    });
    expect(assetTable).toBeVisible();
    expect(within(assetTable).getByText("SPY")).toBeVisible();
    expect(within(assetTable).getByText("QQQ")).toBeVisible();
    expect(screen.getByLabelText("QQQ 本次没有 material outlook")).toBeVisible();
    for (const label of [
      "利率与美联储",
      "经济与通胀",
      "流动性与融资",
      "信用市场",
      "波动率",
      "大类资产与期货",
    ]) {
      expect(screen.getAllByRole("link", { name: new RegExp(label) }).length).toBeGreaterThan(0);
    }
    expect(await axe(view.container)).toHaveNoViolations();
    await waitFor(() =>
      expect(apiMock.readApi).toHaveBeenCalledWith("/api/macro/overview", { token: "secret" }),
    );
  }, 10_000);

  it.each([
    ["/macro/rates-fed", "/api/macro/rates-fed", "利率与美联储"],
    ["/macro/economy-inflation", "/api/macro/economy-inflation", "经济与通胀"],
    ["/macro/liquidity-funding", "/api/macro/liquidity-funding", "流动性与融资"],
    ["/macro/credit", "/api/macro/credit", "信用市场"],
    ["/macro/volatility", "/api/macro/volatility", "波动率"],
    ["/macro/cross-asset", "/api/macro/cross-asset", "大类资产与期货"],
  ] as const)("renders typed module route %s", async (route, apiPath, title) => {
    renderAppRoute(route);

    expect(await screen.findByRole("heading", { level: 1, name: title })).toBeVisible();
    expect(screen.getByRole("complementary", { name: "图表决策注释" })).toBeVisible();
    expect(screen.getByText(/required 历史 完整/)).toBeVisible();
    await waitFor(() => expect(apiMock.readApi).toHaveBeenCalledWith(apiPath, { token: "secret" }));
  });

  it("keeps official CFE expiry in volatility instead of cross-asset", async () => {
    window.location.hash = "#term";
    const volatility = renderAppRoute("/macro/volatility#term");
    expect(await screen.findByText("VXQ26")).toBeVisible();
    expect(screen.getByText("2026-08-19")).toBeVisible();
    volatility.unmount();

    window.location.hash = "#futures";
    renderAppRoute("/macro/cross-asset#futures");
    expect(await screen.findByRole("heading", { name: "期货市场与仓位确认" })).toBeVisible();
    expect(screen.queryByText("VXQ26")).toBeNull();
  });

  it("renders current v2 publication, deterministic delta, replay, and audit", async () => {
    const view = renderAppRoute("/macro/research");

    expect(
      await screen.findByRole("heading", { level: 1, name: "Macro Thesis 档案" }),
    ).toBeVisible();
    expect(
      screen.getByRole("heading", {
        level: 2,
        name: "真实利率回落正在缓和风险资产的贴现压力",
      }),
    ).toBeVisible();
    expect(screen.getByRole("heading", { name: "条件化跟踪 · 正在确认" })).toBeVisible();
    expect(screen.getByText("仅评估 1W / 1M material outlook")).toBeVisible();
    expect(screen.getByText("证据、缺口与生成身份")).toBeVisible();
    expect(screen.getByText("response-1")).toBeVisible();
    expect(await axe(view.container)).toHaveNoViolations();
  });

  it("reads an explicit archive without current Live Delta or Outcome Replay", async () => {
    const thesis = macroThesisFixture({
      publication_id: "macro-publication-2026-07-21",
      session_date: "2026-07-21",
    });
    configureMacroApi({
      schema_version: "macro_thesis_archive_detail_v2",
      state: "historical",
      requested_session_date: "2026-07-21",
      current_session_date: "2026-07-28",
      reason: null,
      thesis,
      run: null,
      history: macroResearchFixture().history,
      recovery: macroOverviewFixture().recovery,
    });

    renderAppRoute("/macro/research?session_date=2026-07-21");

    expect(await screen.findByText("显式历史档案")).toBeVisible();
    expect(
      screen.getByRole("heading", {
        name: "真实利率回落正在缓和风险资产的贴现压力",
      }),
    ).toBeVisible();
    expect(screen.queryByRole("heading", { name: /正在确认/ })).toBeNull();
    expect(screen.queryByText(/仅评估 1W/)).toBeNull();
    expect(screen.getByRole("heading", { name: "发布时缺口与当前事实分开看" })).toBeVisible();
    expect(screen.getByRole("table", { name: "发布时与当前事实恢复矩阵" })).toBeVisible();
  });

  it.each([
    ["running", "生成中"],
    ["retryable", "等待重试"],
    ["failed", "失败"],
    ["config_error", "配置错误"],
    ["not_published", "未发布"],
    ["missing", "尚无运行"],
  ] as const)("renders exact current state %s", async (state, label) => {
    configureMacroApi(macroResearchFixture(state));
    renderAppRoute("/macro/research");

    expect((await screen.findAllByText(label))[0]).toBeVisible();
    expect(
      screen.getByText(
        state === "running"
          ? "Thin Agent 正在执行本次唯一模型调用。"
          : "今日研究未发布；当前读取不会回退到历史主线。",
      ),
    ).toBeVisible();
    expect(screen.queryByText("当前 session 未发布")).toBeNull();
    expect(
      screen.queryByRole("heading", {
        name: "真实利率回落正在缓和风险资产的贴现压力",
      }),
    ).toBeNull();
  });

  it("shows terminal contract failure without a misleading retry fraction or pre-draft label", async () => {
    configureMacroApi(macroResearchFixture("not_published"));
    renderAppRoute("/macro/research");

    expect(await screen.findByText("第1次候选 · terminal contract failure")).toBeVisible();
    expect(
      screen.getByText(/contract_validity · macro_thesis_contract_binding_invalid/),
    ).toBeVisible();
    expect(screen.getByText(`sha256:${"a".repeat(64)}`)).toBeVisible();
    expect(screen.queryByText("1/2")).toBeNull();
    expect(screen.queryByText(/pre-draft/)).toBeNull();
  });
});

function configureMacroApi(data: MacroResearchReadData) {
  setupAppRouteTest((mock) => {
    mockLiveRadarRoute(mock);
    const baseGetApi = mock.getApiImpl;
    mock.getApiImpl = async (path, options) => {
      if (path === "/api/macro/overview") return ok(macroOverviewFixture());
      if (path === "/api/macro/rates-fed") return ok(macroModuleFixture("rates_fed"));
      if (path === "/api/macro/economy-inflation") {
        return ok(macroModuleFixture("economy_inflation"));
      }
      if (path === "/api/macro/liquidity-funding") {
        return ok(macroModuleFixture("liquidity_funding"));
      }
      if (path === "/api/macro/credit") return ok(macroModuleFixture("credit"));
      if (path === "/api/macro/volatility") return ok(macroModuleFixture("volatility"));
      if (path === "/api/macro/cross-asset") return ok(macroModuleFixture("cross_asset"));
      if (path === "/api/macro/research") return ok(data);
      return baseGetApi(path, options);
    };
  });
}
