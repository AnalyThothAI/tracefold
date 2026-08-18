import { cleanup, screen } from "@testing-library/react";
import { renderAppRoute } from "@tests/render/renderRoute";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { setupAppRouteTest } from "./routeTestSetup";

describe("index landing route", () => {
  afterEach(cleanup);
  beforeEach(() => setupAppRouteTest());

  it("redirects the root path to the News landing view", async () => {
    renderAppRoute("/");

    expect(await screen.findByRole("heading", { name: "新闻事件流" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Radar" })).not.toBeInTheDocument();
  });

  it.each(["/radar", "/search?q=PEPE", "/token/Asset/asset%3Adex%3Aeth%3A0xabc"])(
    "routes the retired %s path to the application not-found surface",
    async (path) => {
      renderAppRoute(path);

      expect(await screen.findByText("404 Not Found")).toBeInTheDocument();
      expect(screen.queryByRole("region", { name: "Search Intel" })).not.toBeInTheDocument();
      expect(screen.queryByRole("region", { name: "Token case" })).not.toBeInTheDocument();
    },
  );
});
