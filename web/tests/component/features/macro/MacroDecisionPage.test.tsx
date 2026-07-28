import { MacroModulePage, MacroOverviewPage } from "@features/macro";
import { cleanup, screen } from "@testing-library/react";
import { macroModuleFixture, macroOverviewFixture } from "@tests/fixtures/macroFixture";
import { server } from "@tests/msw/server";
import { renderWithProviders } from "@tests/render/renderWithProviders";
import { http, HttpResponse } from "msw";
import { afterEach, describe, expect, it } from "vitest";

describe("Macro decision workbench", () => {
  afterEach(cleanup);

  it("renders the fixed daily judgment and six module summaries", async () => {
    server.use(
      http.get(/.*\/api\/macro\/overview$/, () =>
        HttpResponse.json({ ok: true, data: macroOverviewFixture() }),
      ),
    );
    renderWithProviders(<MacroOverviewPage token="test-token" />, { route: "/macro" });

    expect(await screen.findByText("分项压力、尚未共振")).toBeVisible();
    expect(screen.getByText("固定资产方向")).toBeVisible();
    expect(screen.getAllByText("覆盖 部分").length).toBeGreaterThan(0);
    expect(screen.getAllByText("数据 当前").length).toBeGreaterThan(0);
    expect(screen.getAllByText("判断 已发布").length).toBeGreaterThan(0);
    expect(screen.getByRole("link", { name: /利率与美联储/ })).toBeVisible();
    expect(screen.queryByText("历史窗口")).toBeNull();
  });

  it("renders the persisted root cause when the daily judgment is blocked", async () => {
    const overview = macroOverviewFixture();
    overview.daily_judgment = null;
    overview.judgment_state = "blocked";
    overview.judgment_status = {
      session_date: "2026-07-27",
      judgment_cutoff_ms: overview.judgment_cutoff_ms ?? overview.read_at_ms,
      state: "blocked",
      reason_code: "critical_evidence_blocked",
      details: {
        blocked_modules: ["rates_fed"],
        modules: [
          {
            module_id: "rates_fed",
            dataset_gaps: [
              {
                dataset_id: "federal_reserve.document.analysis",
                label: "政策文件不可变分析",
                state: "backfilling",
                reason: "derived_fact_pending",
              },
            ],
          },
        ],
      },
      attempted_at_ms: overview.read_at_ms,
    };
    server.use(
      http.get(/.*\/api\/macro\/overview$/, () => HttpResponse.json({ ok: true, data: overview })),
    );
    renderWithProviders(<MacroOverviewPage token="test-token" />, { route: "/macro" });

    expect(await screen.findByRole("heading", { name: "今日判断尚未发布" })).toBeVisible();
    expect(
      screen.getByText(
        "冻结截点缺少关键证据：利率与美联储—政策文件不可变分析（冻结截点前派生事实未完成）。",
      ),
    ).toBeVisible();
  });

  it("renders the default cross-asset benchmark and fixed ETF matrix", async () => {
    server.use(
      http.get(/.*\/api\/macro\/cross-asset$/, () =>
        HttpResponse.json({ ok: true, data: macroModuleFixture("cross_asset") }),
      ),
    );
    renderWithProviders(<MacroModulePage moduleId="cross_asset" token="test-token" />, {
      route: "/macro/cross-asset",
    });

    expect(await screen.findByRole("heading", { name: "大类资产与期货" })).toBeVisible();
    expect(screen.getByText("WTI Cushing spot")).toBeVisible();
    expect(screen.getAllByText("SPY")[0]).toBeVisible();
    expect(screen.getAllByText("USO")[0]).toBeVisible();
    expect(screen.getByText("展开 Coverage Manifest、Dataset 健康与原始事实")).toBeVisible();
  });

  it("renders five concurrent credit dimensions without a composite score", async () => {
    server.use(
      http.get(/.*\/api\/macro\/credit$/, () =>
        HttpResponse.json({ ok: true, data: macroModuleFixture("credit") }),
      ),
    );
    renderWithProviders(<MacroModulePage moduleId="credit" token="test-token" />, {
      route: "/macro/credit",
    });

    expect(await screen.findByLabelText("信用周期五维结论")).toBeVisible();
    expect(screen.getByText("利差水平与速度")).toBeVisible();
    expect(screen.getByText("绝对融资成本")).toBeVisible();
    expect(screen.getByText("银行供给与需求")).toBeVisible();
    expect(screen.getByText("实现信用质量")).toBeVisible();
    expect(screen.getByText("市场流动性")).toBeVisible();
    expect(screen.queryByText("信用总分")).toBeNull();
  });
});
