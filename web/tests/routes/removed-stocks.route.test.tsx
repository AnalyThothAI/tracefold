import { cleanup, screen } from "@testing-library/react";
import { renderAppRoute } from "@tests/render/renderRoute";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { setupAppRouteTest } from "./routeTestSetup";

describe("removed Stocks route", () => {
  afterEach(cleanup);
  beforeEach(() => setupAppRouteTest());

  it("routes the retired Stocks product to the application not-found surface", async () => {
    renderAppRoute("/stocks");

    expect(await screen.findByText("404 Not Found")).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "US stocks radar" })).not.toBeInTheDocument();
  });
});
