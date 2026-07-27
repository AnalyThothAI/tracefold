import { fireEvent, screen, waitFor, within } from "@testing-library/react";
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
    document.body.replaceChildren();
  });

  beforeEach(() => {
    configureMacroApi(macroResearchFixture());
  });

  it("renders one decision overview with six modules and fixed asset directions", async () => {
    renderAppRoute("/macro");

    expect(await screen.findByRole("heading", { level: 1, name: "每日宏观决策台" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "分项压力、尚未共振" })).toBeVisible();
    expect(screen.getByText("固定资产方向")).toBeVisible();
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
    expect(screen.getByText("mep_fixture")).toBeVisible();
    expect(screen.getByText("pass")).toBeVisible();
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
    expect(screen.getByRole("heading", { name: "矛盾与反证" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "判断失效条件" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "下一检查点" })).toBeVisible();
    expect(screen.getByText("展开原始证据与 Dataset 状态")).toBeVisible();
    await waitFor(() => expect(apiMock.readApi).toHaveBeenCalledWith(apiPath, { token: "secret" }));
  });

  it("makes licensed futures absence explicit on the cross-asset module", async () => {
    renderAppRoute("/macro/cross-asset");

    expect(await screen.findByText("CFE VIX期货官方结算")).toBeVisible();
    expect(screen.getByText(/免费阶段没有合规授权数据/)).toBeVisible();
  });

  it("renders one persisted Evidence-Pack research document", async () => {
    renderAppRoute("/macro/research");

    expect(await screen.findByRole("heading", { level: 1, name: "宏观研究工作台" })).toBeVisible();
    expect(screen.getByRole("heading", { level: 2, name: "完成交易日宏观研究" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "利率" })).toBeVisible();
    const citations = screen.getByRole("heading", { name: "引用与事实溯源" }).closest("section");
    expect(citations).not.toBeNull();
    expect(within(citations!).getByText("Evidence Pack / 利率与美联储")).toBeVisible();
    await waitFor(() =>
      expect(apiMock.readApi).toHaveBeenCalledWith("/api/macro/research", { token: "secret" }),
    );
  });

  it("keeps audit metadata collapsed until requested", async () => {
    renderAppRoute("/macro/research");

    await screen.findByRole("heading", { name: "利率" });
    const details = document.querySelector("details.macro-research-audit");
    expect(details).not.toHaveAttribute("open");
    fireEvent.click(screen.getByText("审阅与运行审计"));
    expect(details).toHaveAttribute("open");
    expect(screen.getByText("已复核引用闭合。")).toBeVisible();
  });

  it.each([
    ["generating", "研究正在生成", "页面只轮询持久化状态"],
    ["failed", "本次研究生成失败", "service unavailable"],
    ["missing", "该交易日尚无宏观研究", "选择其他已完成交易日"],
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
