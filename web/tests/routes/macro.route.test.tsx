import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import {
  macroModuleFixture,
  macroOverviewFixture,
  macroResearchFixture,
} from "@tests/fixtures/macroFixture";
import { ok } from "@tests/msw/fixtures";
import { mockLiveRadarRoute } from "@tests/msw/scenarios";
import { renderAppRoute } from "@tests/render/renderRoute";
import { axe } from "jest-axe";
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

  it("renders one Thesis overview with claim-linked evidence and server-grouped assets", async () => {
    renderAppRoute("/macro");

    expect(await screen.findByRole("heading", { level: 1, name: "每日宏观主线" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "实际利率上行主导短期风险资产定价" })).toBeVisible();
    expect(screen.getByText("十二资产：事实动量 vs 条件展望")).toBeVisible();
    expect(screen.getByRole("heading", { name: "主线证据入口" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "Evidence Health" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "Actionable" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "Watch" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "Evidence gap" })).toBeVisible();
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
    expect(screen.getAllByText("正在确认").length).toBeGreaterThan(0);
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
    expect(screen.getByRole("complementary", { name: "图表决策注释" })).toBeVisible();
    expect(screen.getByText("矛盾")).toBeVisible();
    expect(screen.getByText("失效条件")).toBeVisible();
    expect(screen.getByText("下一检查点")).toBeVisible();
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
    expect(screen.getByRole("link", { name: "返回主线总览" })).toHaveAttribute("href", "/macro");
    expect(screen.getByText("CLAIM 1")).toBeVisible();
    expect(screen.getByRole("heading", { name: "资产影响" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "关联主线失效条件" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "关联下一检查点" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "判断" })).toBeVisible();
    expect(screen.getByText("mth_fixture")).toBeVisible();
    expect(screen.getAllByRole("link", { name: /查看来源/ }).length).toBeGreaterThan(0);
    expect(screen.queryByText(/发布方向 偏空/)).toBeNull();
    for (const summary of screen.getAllByText(/资产结果（\d+）/)) {
      fireEvent.click(summary);
    }
    await waitFor(() => expect(screen.getAllByText(/发布方向 偏空/).length).toBeGreaterThan(0));
    expect(screen.queryByText(/来源：决策主源/)).toBeNull();
    expect(screen.queryByText(/来源角色 decision_primary/)).toBeNull();
    expect(screen.getAllByText("horizon_not_expired")[0]).not.toBeVisible();
    expect(document.querySelector(".macro-research-claim")).not.toHaveTextContent(
      "Dataset fixture.rates_fed",
    );
    expect(document.querySelector(".macro-research-document-header")).not.toHaveTextContent(
      "mep_fixture",
    );
    expect(screen.queryByText(/Evidence ref macro-module:/)).toBeNull();
    expect(screen.getByRole("heading", { name: "不可变主线历史" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "生成尝试（不属于档案历史）" })).toBeVisible();
    expect(
      document.querySelectorAll('.macro-research-status-glyph[aria-hidden="true"]').length,
    ).toBeGreaterThan(0);
    expect(screen.getByRole("main", { name: "Macro Thesis 档案" })).not.toHaveTextContent(
      /\bno_call\b/,
    );
    const dossier = document.querySelector(".macro-research-document");
    expect(dossier).not.toBeNull();
    expect(await axe(dossier!)).toHaveNoViolations();
    await waitFor(() =>
      expect(apiMock.readApi).toHaveBeenCalledWith("/api/macro/research", { token: "secret" }),
    );
  });

  it("keeps audit metadata collapsed until requested", async () => {
    renderAppRoute("/macro/research");

    await screen.findByText("CLAIM 1");
    const details = document.querySelector("details.macro-research-audit");
    expect(details).not.toHaveAttribute("open");
    fireEvent.click(screen.getByText("发布附录：审阅、数据质量与来源谱系"));
    expect(details).toHaveAttribute("open");
    expect(await screen.findByText("证据引用、反证与资产条件已独立复核。")).toBeVisible();
    expect(screen.getByRole("heading", { name: "发布时数据质量" })).toBeVisible();
    expect(screen.getAllByText("完整").length).toBeGreaterThan(0);
    const rawEvidenceRef = screen.getAllByText(/Evidence ref macro-module:/)[0]!;
    expect(rawEvidenceRef).not.toBeVisible();
    fireEvent.click(screen.getAllByText("查看引用技术身份")[0]!);
    expect(rawEvidenceRef).toBeVisible();

    fireEvent.click(screen.getByText("Reconciliation receipts（1）"));
    expect(await screen.findByText("容差内一致：差异 +0.01%，容差 0.02%")).toBeVisible();

    fireEvent.click(screen.getByText("完整来源谱系（6）"));
    const lineage = await screen.findByRole("list", { name: "Evidence Pack 发布时来源谱系" });
    expect(lineage).toBeVisible();
    expect(within(lineage).getAllByText("事实观测")).toHaveLength(6);
    expect(within(lineage).getAllByText("来源发布")).toHaveLength(6);
    expect(within(lineage).getAllByText("系统接收")).toHaveLength(6);
    expect(within(lineage).getAllByRole("link", { name: "原始来源" })).toHaveLength(6);
    expect(within(lineage).getAllByText(/· 决策主源/)).toHaveLength(6);
  });

  it("keeps historical dossiers frozen and excludes current follow-up layers", async () => {
    configureMacroApi(macroResearchFixture("historical"));
    renderAppRoute("/macro/research?session_date=2026-07-27");

    expect(await screen.findByRole("heading", { name: "历史档案边界" })).toBeVisible();
    expect(screen.getByText(/只呈现发布时冻结的结论、数据质量与对账回执/)).toBeVisible();
    expect(screen.queryByRole("heading", { name: "当前观察层" })).toBeNull();
    expect(screen.queryByRole("heading", { name: "Outcome Replay" })).toBeNull();

    fireEvent.click(screen.getByText("发布附录：审阅、数据质量与来源谱系"));
    expect(await screen.findByRole("heading", { name: "发布时数据质量" })).toBeVisible();
    expect(screen.getByText("Reconciliation receipts（1）")).toBeVisible();
  });

  it.each([
    ["generating", "Macro Thesis 正在生成", "后台研究运行尚未完成"],
    ["failed", "本次 Macro Thesis 未发布", "研究提供方暂不可用"],
    ["missing", "该交易日尚无 Macro Thesis", "该交易日尚无已发布主线"],
  ] as const)("renders persisted %s state", async (state, title, hint) => {
    configureMacroApi(macroResearchFixture(state));
    renderAppRoute("/macro/research");

    expect(await screen.findByText(title)).toBeVisible();
    expect(screen.getAllByText(new RegExp(hint)).length).toBeGreaterThan(0);
    if (state === "failed") {
      expect(screen.getByText("需要操作员处理")).toBeVisible();
      expect(screen.getByText("否")).toBeVisible();
      expect(screen.getByText(/最后状态变化/)).toBeVisible();
      expect(screen.queryByText("macro_thesis_configuration_error")).toBeNull();
    }
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
