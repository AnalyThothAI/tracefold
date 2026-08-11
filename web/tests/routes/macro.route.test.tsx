import { screen, waitFor } from "@testing-library/react";
import { macroModuleFixture, macroOverviewFixture } from "@tests/fixtures/macroFixture";
import { ok } from "@tests/msw/fixtures";
import { mockLiveRadarRoute } from "@tests/msw/scenarios";
import { renderAppRoute } from "@tests/render/renderRoute";
import { beforeEach, describe, expect, it } from "vitest";

import { apiMock, setupAppRouteTest } from "./routeTestSetup";

describe("Macro current-fact routes", () => {
  beforeEach(() => {
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
        return baseGetApi(path, options);
      };
    });
  });

  it("renders the overview from the current module API", async () => {
    renderAppRoute("/macro");

    expect(await screen.findByRole("heading", { name: "宏观事实总览" })).toBeVisible();
    await waitFor(() =>
      expect(apiMock.readApi).toHaveBeenCalledWith("/api/macro/overview", { token: "secret" }),
    );
  });

  it("renders /macro/overview as an alias of the current overview", async () => {
    renderAppRoute("/macro/overview");

    expect(await screen.findByRole("heading", { name: "宏观事实总览" })).toBeVisible();
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

    expect(await screen.findByRole("heading", { name: title })).toBeVisible();
    await waitFor(() => expect(apiMock.readApi).toHaveBeenCalledWith(apiPath, { token: "secret" }));
  });
});
