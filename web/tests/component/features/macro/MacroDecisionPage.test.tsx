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
    expect(screen.getByText("今日最重要变化")).toBeVisible();
    expect(screen.getByText("判断失效条件")).toBeVisible();
    expect(screen.getByText(/CME利率期货曲线：免费阶段没有合规授权数据/)).toBeVisible();
    expect(screen.getByRole("link", { name: /利率与美联储/ })).toBeVisible();
    expect(screen.queryByText("历史窗口")).toBeNull();
  });

  it("renders one typed module with semantic changes, charts, gaps and raw evidence", async () => {
    server.use(
      http.get(/.*\/api\/macro\/cross-asset$/, () =>
        HttpResponse.json({ ok: true, data: macroModuleFixture("cross_asset") }),
      ),
    );
    renderWithProviders(<MacroModulePage moduleId="cross_asset" token="test-token" />, {
      route: "/macro/cross-asset",
    });

    expect(await screen.findByRole("heading", { name: "大类资产与期货" })).toBeVisible();
    expect(screen.getByText("CFE VIX期货官方结算")).toBeVisible();
    expect(screen.getByText(/免费阶段没有合规授权数据/)).toBeVisible();
    expect(screen.getByText("展开原始证据与 Dataset 状态")).toBeVisible();
  });
});
