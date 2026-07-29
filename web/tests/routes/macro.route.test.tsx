import { fireEvent, screen, waitFor } from "@testing-library/react";
import {
  macroModuleFixture,
  macroOverviewFixture,
  macroResearchFixture,
} from "@tests/fixtures/macroFixture";
import { ok } from "@tests/msw/fixtures";
import { mockLiveRadarRoute } from "@tests/msw/scenarios";
import { renderAppRoute } from "@tests/render/renderRoute";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { apiMock, setupAppRouteTest } from "./routeTestSetup";

describe("daily macro decision workbench", () => {
  afterEach(() => {
    window.location.hash = "";
    document.body.replaceChildren();
  });

  beforeEach(() => {
    configureMacroApi(macroResearchFixture());
  });

  it("renders one Thesis overview with six modules and fixed asset views", async () => {
    renderAppRoute("/macro");

    expect(await screen.findByRole("heading", { level: 1, name: "每日宏观主线" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "实际利率上行主导短期风险资产定价" })).toBeVisible();
    expect(screen.getByText("十二资产：事实动量 vs 条件展望")).toBeVisible();
    for (const label of [
      "利率与美联储",
      "经济与通胀",
      "流动性与融资",
      "信用市场",
      "波动率",
      "大类资产与期货",
    ]) {
      expect(screen.getByRole("heading", { name: label })).toBeVisible();
    }
    expect(screen.getByText("确认主线")).toBeVisible();
    expect(screen.queryByText("历史窗口")).toBeNull();
    await waitFor(() =>
      expect(apiMock.readApi).toHaveBeenCalledWith("/api/macro/overview", { token: "secret" }),
    );
  });

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
    expect(screen.getByRole("heading", { name: "矛盾" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "失效条件" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "下一检查点" })).toBeVisible();
    expect(
      screen.getByText("展开 Coverage、Current Health、History Depth 与原始事实"),
    ).toBeVisible();
    await waitFor(() => expect(apiMock.readApi).toHaveBeenCalledWith(apiPath, { token: "secret" }));
  });

  it("renders futures confirmation in the fixed cross-asset route", async () => {
    window.location.hash = "#futures";
    renderAppRoute("/macro/cross-asset#futures");

    expect(await screen.findByRole("heading", { name: "期货市场与仓位确认" })).toBeVisible();
    expect(screen.getByText(/^VIX 官方结算/)).toBeVisible();
  });

  it("renders the same persisted Thesis in its immutable history page", async () => {
    renderAppRoute("/macro/research");

    expect(
      await screen.findByRole("heading", { level: 1, name: "Macro Thesis 档案" }),
    ).toBeVisible();
    expect(
      screen.getByRole("heading", { level: 2, name: "实际利率上行主导短期风险资产定价" }),
    ).toBeVisible();
    expect(screen.getByRole("heading", { name: "主线论点与因果链" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "不可变主线历史" })).toBeVisible();
    await waitFor(() =>
      expect(apiMock.readApi).toHaveBeenCalledWith("/api/macro/research", { token: "secret" }),
    );
  });

  it("keeps audit metadata collapsed until requested", async () => {
    renderAppRoute("/macro/research");

    await screen.findByRole("heading", { name: "主线论点与因果链" });
    const details = document.querySelector("details.macro-research-audit");
    expect(details).not.toHaveAttribute("open");
    fireEvent.click(screen.getByText("独立审阅与运行审计"));
    expect(details).toHaveAttribute("open");
    expect(screen.getByText("证据引用、反证与资产条件已独立复核。")).toBeVisible();
  });

  it.each([
    ["generating", "Macro Thesis 正在生成", "页面只轮询持久化状态"],
    ["failed", "本次 Macro Thesis 未发布", "service unavailable"],
    ["missing", "该交易日尚无 Macro Thesis", "选择其他交易日"],
  ] as const)("renders persisted %s state", async (state, title, hint) => {
    configureMacroApi(macroResearchFixture(state));
    renderAppRoute("/macro/research");

    expect(await screen.findByText(title)).toBeVisible();
    expect(screen.getByText(new RegExp(hint))).toBeVisible();
  });
});

function configureMacroApi(data: ReturnType<typeof macroResearchFixture>) {
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
