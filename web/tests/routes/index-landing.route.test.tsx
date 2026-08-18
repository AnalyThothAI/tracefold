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

  it("routes the retired Radar path to the application not-found surface", async () => {
    renderAppRoute("/radar");

    expect(await screen.findByText("404 Not Found")).toBeInTheDocument();
  });
});
