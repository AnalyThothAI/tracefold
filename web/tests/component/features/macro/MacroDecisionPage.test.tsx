import { MacroModulePage, MacroOverviewPage } from "@features/macro";
import { cleanup, screen } from "@testing-library/react";
import { macroModuleFixture, macroOverviewFixture } from "@tests/fixtures/macroFixture";
import { server } from "@tests/msw/server";
import { renderWithProviders } from "@tests/render/renderWithProviders";
import { http, HttpResponse } from "msw";
import { afterEach, describe, expect, it } from "vitest";

describe("Macro Thesis workbench", () => {
  afterEach(() => {
    cleanup();
    window.history.replaceState(null, "", window.location.pathname);
  });

  it("renders one mainline, tensions, twelve asset views, live delta and six module roles", async () => {
    server.use(
      http.get(/.*\/api\/macro\/overview$/, () =>
        HttpResponse.json({ ok: true, data: macroOverviewFixture() }),
      ),
    );
    renderWithProviders(<MacroOverviewPage token="test-token" />, { route: "/macro" });

    expect(
      await screen.findByRole("heading", { name: "实际利率上行主导短期风险资产定价" }),
    ).toBeVisible();
    expect(screen.getByRole("heading", { name: "当前核心矛盾" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "十二资产：事实动量 vs 条件展望" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "Live Delta" })).toBeVisible();
    expect(screen.getByText("确认主线")).toBeVisible();
    expect(screen.getByRole("heading", { name: "数据质量与真实缺口" })).toBeVisible();
    expect(screen.queryByLabelText("影响当前判断的异常状态")).toBeNull();
    expect(screen.getByRole("link", { name: /利率与美联储/ })).toBeVisible();
  });

  it("keeps the current session empty instead of backfilling a prior thesis", async () => {
    const overview = macroOverviewFixture();
    overview.thesis = null;
    overview.thesis_state = "missing";
    overview.live_delta = null;
    overview.outcome_replay = null;
    server.use(
      http.get(/.*\/api\/macro\/overview$/, () => HttpResponse.json({ ok: true, data: overview })),
    );
    renderWithProviders(<MacroOverviewPage token="test-token" />, { route: "/macro" });

    expect(
      await screen.findByRole("heading", { name: "2026-07-27 Macro Thesis 尚未发布" }),
    ).toBeVisible();
    expect(screen.getByText(/不会用前一交易日结论填充当前主线/)).toBeVisible();
    expect(screen.getByText("Thesis 截点")).toBeVisible();
  });

  it("renders source-separated cross-asset facts without identity blending", async () => {
    server.use(
      http.get(/.*\/api\/macro\/cross-asset$/, () =>
        HttpResponse.json({ ok: true, data: macroModuleFixture("cross_asset") }),
      ),
    );
    renderWithProviders(<MacroModulePage moduleId="cross_asset" token="test-token" />, {
      route: "/macro/cross-asset",
    });

    expect(await screen.findByRole("heading", { name: "大类资产与期货" })).toBeVisible();
    expect(screen.getByText("Decision Primary")).toBeInTheDocument();
    expect(screen.getByText("Intraday Proxy")).toBeInTheDocument();
    expect(screen.getAllByText("separate_source_facts_no_blend").length).toBeGreaterThan(0);
    expect(
      screen.getByText("展开 Coverage、Current Health、History Depth 与原始事实"),
    ).toBeVisible();
  });

  it("groups normalized cross-asset history into three decision-readable charts", async () => {
    window.location.hash = "#normalized";
    server.use(
      http.get(/.*\/api\/macro\/cross-asset$/, () =>
        HttpResponse.json({ ok: true, data: macroModuleFixture("cross_asset") }),
      ),
    );
    renderWithProviders(<MacroModulePage moduleId="cross_asset" token="test-token" />, {
      route: "/macro/cross-asset#normalized",
    });

    expect(await screen.findByRole("heading", { name: "大类资产分组归一化走势" })).toBeVisible();
    expect(screen.getByText("权益")).toBeVisible();
    expect(screen.getByText("久期与信用")).toBeVisible();
    expect(screen.getByText("美元与商品")).toBeVisible();
    expect(screen.queryByText("其他资产")).toBeNull();
  });

  it("renders four credit dimensions and no paid-data placeholder", async () => {
    server.use(
      http.get(/.*\/api\/macro\/credit$/, () =>
        HttpResponse.json({ ok: true, data: macroModuleFixture("credit") }),
      ),
    );
    renderWithProviders(<MacroModulePage moduleId="credit" token="test-token" />, {
      route: "/macro/credit",
    });

    expect(await screen.findByText("利差水平与速度")).toBeVisible();
    expect(screen.getAllByText("绝对融资成本").length).toBeGreaterThan(0);
    expect(screen.getByText("银行供给与需求")).toBeVisible();
    expect(screen.getByText("实现信用质量")).toBeVisible();
    expect(screen.queryByText(/TRACE|licensed_unavailable/)).toBeNull();
  });

  it("hard-loads only the hash-selected module category", async () => {
    window.location.hash = "#policy";
    server.use(
      http.get(/.*\/api\/macro\/rates-fed$/, () =>
        HttpResponse.json({ ok: true, data: macroModuleFixture("rates_fed") }),
      ),
    );
    renderWithProviders(<MacroModulePage moduleId="rates_fed" token="test-token" />, {
      route: "/macro/rates-fed#policy",
    });

    expect(await screen.findByRole("heading", { name: "政策走廊与当前市场定价" })).toBeVisible();
    expect(screen.queryByText("名义 Treasury 曲线")).toBeNull();
    expect(screen.getByLabelText("利率与美联储当前视图")).toHaveValue("policy");
  });
});
