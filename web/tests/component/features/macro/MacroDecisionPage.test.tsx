import { MacroModulePage, MacroOverviewPage } from "@features/macro";
import { cleanup, screen } from "@testing-library/react";
import { macroModuleFixture, macroOverviewFixture } from "@tests/fixtures/macroFixture";
import { server } from "@tests/msw/server";
import { renderWithProviders } from "@tests/render/renderWithProviders";
import { http, HttpResponse } from "msw";
import { afterEach, describe, expect, it } from "vitest";

const SESSION_PROPS = {
  bootstrapError: false,
  bootstrapLoading: false,
  token: "test-token",
};

describe("Macro current-fact workbench", () => {
  afterEach(() => cleanup());

  it("renders the six current modules without a Thesis dependency", async () => {
    server.use(
      http.get(/.*\/api\/macro\/overview$/, () =>
        HttpResponse.json({ ok: true, data: macroOverviewFixture() }),
      ),
    );

    renderWithProviders(<MacroOverviewPage {...SESSION_PROPS} />, { route: "/macro" });

    expect(await screen.findByRole("heading", { name: "宏观事实总览" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "当前事实摘要" })).toBeVisible();
    expect(screen.getAllByText("CURRENT FACTS")).toHaveLength(6);
  });

  it("renders the rates module directly from its persisted read model", async () => {
    server.use(
      http.get(/.*\/api\/macro\/rates-fed$/, () =>
        HttpResponse.json({ ok: true, data: macroModuleFixture("rates_fed") }),
      ),
    );

    renderWithProviders(
      <MacroModulePage {...SESSION_PROPS} moduleId="rates_fed" />,
      { route: "/macro/rates-fed" },
    );

    expect(await screen.findByRole("heading", { name: "利率与美联储" })).toBeVisible();
    expect(screen.getByText(/确定性事实页/)).toBeVisible();
  });
});
